"""MessagePipeline - Integrates deduplication, reordering, gap fill, and flow control."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from agentrux.sdk.deduplicator import Deduplicator
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.flow_controller import FlowController
from agentrux.sdk.reorder_buffer import ReorderBuffer
from agentrux.sdk.stats import PipelineStats

if TYPE_CHECKING:
    from agentrux.sdk.gap_detector import GapDetector

logger = logging.getLogger("agentrux.sdk.pipeline")


class MessagePipeline:
    """Deduplicator -> ReorderBuffer -> (GapDetector) -> FlowController.

    GapDetector is optional. When provided, the pipeline checks for gaps
    after each insert and backfills them via the by-sequence REST API.
    Backfilled events are routed back through Dedup -> Reorder so the
    FlowController only ever sees a single ordered stream.
    """

    def __init__(
        self,
        deduplicator: Deduplicator | None = None,
        reorder_buffer: ReorderBuffer | None = None,
        flow_controller: FlowController | None = None,
        gap_detector: "GapDetector | None" = None,
    ):
        self._dedup = deduplicator or Deduplicator()
        self._reorder = reorder_buffer or ReorderBuffer()
        self._flow = flow_controller or FlowController()
        self._gap_detector = gap_detector
        self._topic_id: str | None = None

    def set_topic_id(self, topic_id: str) -> None:
        self._topic_id = topic_id

    def set_initial_sequence(self, seq: int) -> list[MessageEnvelope]:
        """Set initial expected sequence for the reorder buffer.

        Returns any messages now deliverable from the new position
        (non-empty only on forward jumps after gap-fill).
        """
        return self._reorder.set_initial_sequence(seq)

    async def process(self, msg: MessageEnvelope) -> list[MessageEnvelope]:
        """Process a message through the full pipeline.

        Returns list of messages ready for delivery (in order).

        Order-preservation rule: backfilled events from GapDetector go
        back through Dedup -> Reorder, never directly to FlowController.
        FlowController is the single output of ReorderBuffer.
        """
        # 1. Deduplication
        if self._dedup.is_duplicate(msg.event_id):
            logger.debug("Duplicate dropped: %s", msg.event_id)
            return []
        self._dedup.mark_seen(msg.event_id)

        # 2. Reorder buffer
        deliverable = self._reorder.insert(msg)

        # 3. Gap detection + backfill
        if self._gap_detector and self._topic_id and self._reorder.gaps:
            for (start, end) in list(self._reorder.gaps):
                try:
                    result = await self._gap_detector.fill(
                        self._topic_id, start, end,
                    )
                except Exception as e:
                    logger.warning("Gap fill error %d-%d: %s", start, end, e)
                    continue

                # Re-inject backfilled events through Dedup -> Reorder
                for bf in result.backfilled:
                    if not self._dedup.is_duplicate(bf.event_id):
                        self._dedup.mark_seen(bf.event_id)
                        deliverable.extend(self._reorder.insert(bf))

                # Forward-jump past unrecoverable ranges
                for (m_start, m_end) in result.missing_ranges:
                    try:
                        extra = self._reorder.set_initial_sequence(m_end + 1)
                        deliverable.extend(extra)
                    except ValueError:
                        # Already advanced past this range; safe to ignore
                        pass

        # 4. Flow control (single ordered output)
        for d_msg in deliverable:
            await self._flow.push(d_msg)

        return deliverable

    async def flush(self) -> list[MessageEnvelope]:
        """Force-flush timed-out messages."""
        flushed = self._reorder.flush()
        for msg in flushed:
            if not self._dedup.is_duplicate(msg.event_id):
                self._dedup.mark_seen(msg.event_id)
                await self._flow.push(msg)
        return flushed

    @property
    def stats(self) -> PipelineStats:
        return PipelineStats(
            deduplicator=self._dedup.stats,
            reorder_buffer=self._reorder.stats,
            flow_controller=self._flow.stats,
            gap_detector=self._gap_detector.stats if self._gap_detector else None,
        )
