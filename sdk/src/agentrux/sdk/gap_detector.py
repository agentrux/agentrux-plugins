"""GapDetector - Detects sequence gaps and backfills via REST API.

See doc/詳細設計/05_ビルディングブロック/11_PullPubSub_SDK詳細設計.md §2.4.

Key design points:
- Range backfill via GET /topics/{id}/events/by-sequence
- Partial response (count < expected) means some seqs were physically deleted
  by the retention cleanup job. The missing seqs are reported as
  unrecoverable; the SDK does not retry them.
- Empty response means the entire range is past retention. Same handling.
- max_gap_size cap is enforced server-side (422). Client treats over-limit
  ranges as immediately unrecoverable instead of silently capping.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.stats import GapDetectorStats

if TYPE_CHECKING:
    from agentrux.sdk.client import AgenTruxAPIClient

logger = logging.getLogger("agentrux.sdk.gap_detector")


class GapState(Enum):
    DETECTED = "detected"
    FILLING = "filling"
    FILLED = "filled"
    UNRECOVERABLE = "unrecoverable"


@dataclass
class GapRecord:
    start_seq: int
    end_seq: int
    state: GapState = GapState.DETECTED
    retries: int = 0


@dataclass
class FillResult:
    """Result of a gap fill attempt.

    backfilled: events successfully retrieved (in sequence order)
    missing_ranges: list of (start, end) for seqs that are unrecoverable
    """
    backfilled: list[MessageEnvelope]
    missing_ranges: list[tuple[int, int]]


UnrecoverableCallback = Callable[[int, int, str], Awaitable[None]]


class GapDetector:
    """Detects sequence gaps and backfills via the by-sequence REST API."""

    MAX_GAP_SIZE_DEFAULT = 500  # must match server-side range limit

    def __init__(
        self,
        api_client: "AgenTruxAPIClient",
        *,
        max_backfill_retries: int = 3,
        max_gap_size: int = MAX_GAP_SIZE_DEFAULT,
        on_unrecoverable: UnrecoverableCallback | None = None,
    ):
        self._api = api_client
        self._max_retries = max_backfill_retries
        self._max_gap_size = max_gap_size
        self._on_unrecoverable = on_unrecoverable
        self._pending: dict[tuple[int, int], GapRecord] = {}
        self._stats = GapDetectorStats()

    async def fill(
        self, topic_id: str, start_seq: int, end_seq: int,
    ) -> FillResult:
        """Attempt to fill the gap [start_seq, end_seq] (inclusive).

        Returns FillResult containing successfully retrieved events and
        a list of (start, end) ranges that are unrecoverable.

        Behavior:
        - If range size > max_gap_size: entire range is reported as
          unrecoverable (no API call).
        - If API returns full count: all events backfilled, no missing.
        - If API returns short count (including 0): the missing seqs
          (computed as the contiguous gaps between returned seqs) are
          reported as unrecoverable, and the on_unrecoverable callback
          is fired for each contiguous missing range.
        """
        gap_size = end_seq - start_seq + 1
        self._stats.gaps_detected += 1

        if gap_size > self._max_gap_size:
            logger.warning(
                "Gap %d-%d exceeds max_gap_size=%d: marking unrecoverable",
                start_seq, end_seq, self._max_gap_size,
            )
            await self._notify_unrecoverable(start_seq, end_seq)
            self._stats.gaps_unrecoverable += 1
            return FillResult(backfilled=[], missing_ranges=[(start_seq, end_seq)])

        key = (start_seq, end_seq)
        record = self._pending.get(key) or GapRecord(start_seq, end_seq)
        record.state = GapState.FILLING
        self._pending[key] = record

        try:
            self._stats.backfill_requests += 1
            items = await self._api.list_events_by_sequence(
                topic_id, start_seq, end_seq,
            )
        except Exception as e:
            record.retries += 1
            if record.retries >= self._max_retries:
                record.state = GapState.UNRECOVERABLE
                self._stats.gaps_unrecoverable += 1
                logger.error(
                    "Backfill exhausted retries for gap %d-%d: %s",
                    start_seq, end_seq, e,
                )
                await self._notify_unrecoverable(start_seq, end_seq)
                self._pending.pop(key, None)
                return FillResult(
                    backfilled=[], missing_ranges=[(start_seq, end_seq)],
                )
            logger.warning(
                "Backfill retry %d/%d for gap %d-%d: %s",
                record.retries, self._max_retries, start_seq, end_seq, e,
            )
            return FillResult(backfilled=[], missing_ranges=[])

        # Parse envelopes
        backfilled: list[MessageEnvelope] = []
        for item in items:
            try:
                backfilled.append(MessageEnvelope.from_api_response(item))
            except Exception:
                logger.exception("Failed to parse backfill item")
                continue

        backfilled.sort(key=lambda m: m.sequence_no)

        # Compute missing ranges (the gap between expected seqs and what we got)
        present_seqs = {m.sequence_no for m in backfilled}
        missing_seqs = sorted(
            seq for seq in range(start_seq, end_seq + 1)
            if seq not in present_seqs
        )
        missing_ranges = _coalesce_ranges(missing_seqs)

        if missing_ranges:
            record.state = GapState.UNRECOVERABLE
            self._stats.gaps_unrecoverable += 1
            for (m_start, m_end) in missing_ranges:
                await self._notify_unrecoverable(m_start, m_end)
        else:
            record.state = GapState.FILLED
            self._stats.gaps_filled += 1

        self._pending.pop(key, None)
        return FillResult(backfilled=backfilled, missing_ranges=missing_ranges)

    async def _notify_unrecoverable(self, start: int, end: int) -> None:
        if self._on_unrecoverable is None:
            return
        try:
            await self._on_unrecoverable(start, end, "unrecoverable")
        except Exception:
            logger.exception("on_unrecoverable callback raised")

    @property
    def stats(self) -> GapDetectorStats:
        return self._stats


def _coalesce_ranges(seqs: list[int]) -> list[tuple[int, int]]:
    """Group sorted sequential ints into contiguous (start, end) ranges."""
    if not seqs:
        return []
    out: list[tuple[int, int]] = []
    start = prev = seqs[0]
    for s in seqs[1:]:
        if s == prev + 1:
            prev = s
            continue
        out.append((start, prev))
        start = prev = s
    out.append((start, prev))
    return out
