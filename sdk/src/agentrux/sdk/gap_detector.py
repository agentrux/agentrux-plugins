"""GapDetector - detect sequence-number gaps and try to backfill via pull.

v0.3 design (server has no `by-sequence` endpoint):

  Strategy: when reorder_buffer detects events arriving with a gap
  [start_seq, end_seq], we ask the server `list_events(after=<id of
  the last delivered event>, limit=N)` and look at the returned events'
  sequence_number values. Any seqs in [start, end] that the server
  returns are reinjected through the pipeline; any seqs the server is
  unable to return (because the events were never produced, were
  rejected by validation, or fell off retention) are reported as
  unrecoverable.

  This is "best-effort" — the server has no guarantee that fetching
  forward from before-the-gap returns the gap-events specifically
  (events with mismatched topic or filtered event_type would not appear
  if we accidentally pass `type=` to list_events, which we don't here).

  When `before_event_id` is None (the gap occurs before any event has
  been delivered yet — e.g. the very first SSE hint hits a non-zero
  sequence_number), there is no cursor to pull from, so the entire
  range is reported unrecoverable immediately.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Awaitable, Callable

from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.errors import GapUnrecoverableError
from agentrux.sdk.stats import GapDetectorStats

if TYPE_CHECKING:
    from agentrux.sdk.client import AgenTruxAPIClient

logger = logging.getLogger("agentrux.sdk.gap_detector")


class GapState(Enum):
    DETECTED = "detected"
    PARTIAL = "partial"
    FILLED = "filled"
    UNRECOVERABLE = "unrecoverable"


@dataclass
class GapRecord:
    start_seq: int
    end_seq: int
    state: GapState = GapState.DETECTED


@dataclass
class FillResult:
    """Outcome of a fill attempt.

    backfilled: events successfully retrieved (in seq order).
    missing_ranges: list of (start, end) for seqs that could not be
        fetched within the gap range.
    """

    backfilled: list[MessageEnvelope]
    missing_ranges: list[tuple[int, int]]


UnrecoverableCallback = Callable[[int, int, str], Awaitable[None]]


class GapDetector:
    """Best-effort gap filler using `list_events(after=, limit=)`."""

    # Upper bound on how many events we pull when probing. A real gap
    # is usually 1-2 events (a single in-flight reorder), so 100 covers
    # almost all real cases without overspending bandwidth. Caller can
    # tune via constructor.
    DEFAULT_PROBE_BUFFER = 32

    def __init__(
        self,
        api_client: "AgenTruxAPIClient | None" = None,
        *,
        on_unrecoverable: UnrecoverableCallback | None = None,
        max_probe_events: int = DEFAULT_PROBE_BUFFER,
    ) -> None:
        self._api = api_client
        self._on_unrecoverable = on_unrecoverable
        self._max_probe = max_probe_events
        self._stats = GapDetectorStats()

    # Safety cap: how many pages to follow when chasing a large gap.
    # 16 pages * default 32 events = 512 events. Big enough for any
    # realistic reorder window; bounded so a misconfigured server
    # can't make us paginate forever.
    MAX_PROBE_PAGES = 16

    async def fill(
        self,
        topic_id: str,
        start_seq: int,
        end_seq: int,
        *,
        before_event_id: str | None = None,
    ) -> FillResult:
        """Attempt to fetch events in [start_seq, end_seq] via list_events.

        Iterates `list_events(after=cursor, limit=N)` and follows
        `next.has_more` until either the page covers seq>=end_seq or
        the server says "no more events" or we exhaust MAX_PROBE_PAGES.

        Returns FillResult with whatever could be fetched plus any
        remaining missing ranges. The on_unrecoverable callback (if
        registered) fires once per residual missing range with the
        reason string describing why fetch was incomplete.
        """
        gap_size = end_seq - start_seq + 1
        self._stats.gaps_detected += 1

        # Without an anchor we can't pull "events after <X>" because we
        # don't know X. Report immediately.
        if before_event_id is None or self._api is None:
            return await self._mark_full_range_unrecoverable(
                topic_id, start_seq, end_seq,
                reason=(
                    "no_anchor"
                    if before_event_id is None
                    else "no_api_client"
                ),
            )

        probe_limit = min(self._max_probe, max(gap_size + 16, 8))
        collected: dict[int, MessageEnvelope] = {}
        cursor: str | None = before_event_id
        pages = 0
        reason_for_partial: str = "no_events_in_probe"
        while pages < self.MAX_PROBE_PAGES:
            try:
                page = await self._api.list_events(
                    topic_id=topic_id,
                    after=cursor,
                    limit=probe_limit,
                    order="asc",
                )
            except Exception as exc:  # noqa: BLE001 - probe failure is informative
                logger.warning(
                    "gap_detector probe page %d failed for [%d, %d] on "
                    "topic=%s: %s",
                    pages, start_seq, end_seq, topic_id, exc,
                )
                # If nothing collected so far, treat whole range as
                # unrecoverable; otherwise return partial result and let
                # missing_ranges drive the on_unrecoverable callback.
                if not collected:
                    return await self._mark_full_range_unrecoverable(
                        topic_id, start_seq, end_seq, reason="probe_failed",
                    )
                reason_for_partial = "probe_failed_mid_page"
                break

            for ev in page.events:
                if start_seq <= ev.sequence_number <= end_seq:
                    collected[ev.sequence_number] = ev

            if not page.events:
                break
            last_seq = page.events[-1].sequence_number
            # Server passed end_seq → no more in-range events possible.
            if last_seq >= end_seq:
                break
            if not page.next.has_more or page.next.after is None:
                break
            # Advance to the next page using the page's own cursor.
            cursor = page.next.after
            pages += 1

        if pages == self.MAX_PROBE_PAGES:
            reason_for_partial = "probe_truncated_max_pages"

        backfilled = [collected[s] for s in sorted(collected)]
        missing_seqs = [s for s in range(start_seq, end_seq + 1) if s not in collected]
        missing_ranges = _coalesce_ranges(missing_seqs)

        if backfilled and not missing_ranges:
            self._stats.gaps_filled += 1
        elif backfilled:
            # Partial fill: prefer the more specific reason captured
            # during pagination (probe_failed_mid_page or
            # probe_truncated_max_pages) over the generic "partial_fill"
            # default. Codex 3rd review #1.
            self._stats.gaps_filled += 1
            self._stats.gaps_unrecoverable += 1
            partial_reason = (
                reason_for_partial
                if reason_for_partial != "no_events_in_probe"
                else "partial_fill"
            )
            for (m_start, m_end) in missing_ranges:
                await self._notify_unrecoverable(
                    m_start, m_end, partial_reason
                )
        else:
            self._stats.gaps_unrecoverable += 1
            await self._notify_unrecoverable(
                start_seq, end_seq, reason_for_partial
            )

        return FillResult(backfilled=backfilled, missing_ranges=missing_ranges)

    async def _mark_full_range_unrecoverable(
        self, topic_id: str, start_seq: int, end_seq: int, *, reason: str
    ) -> FillResult:
        self._stats.gaps_unrecoverable += 1
        logger.warning(
            "Unrecoverable sequence gap on topic=%s seq=[%d, %d] (reason=%s)",
            topic_id, start_seq, end_seq, reason,
        )
        if self._on_unrecoverable is None:
            raise GapUnrecoverableError(
                f"Sequence gap [{start_seq}, {end_seq}] on topic {topic_id} "
                f"is unrecoverable (reason={reason}). Register on_unrecoverable= "
                "to handle gracefully.",
                gap_start_seq=start_seq,
                gap_end_seq=end_seq,
            )
        await self._notify_unrecoverable(start_seq, end_seq, reason)
        return FillResult(backfilled=[], missing_ranges=[(start_seq, end_seq)])

    async def _notify_unrecoverable(self, start: int, end: int, reason: str) -> None:
        if self._on_unrecoverable is None:
            return
        try:
            await self._on_unrecoverable(start, end, reason)
        except Exception:  # noqa: BLE001 - user callback isolation
            logger.exception(
                "on_unrecoverable callback raised for [%d, %d] (reason=%s)",
                start, end, reason,
            )

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
