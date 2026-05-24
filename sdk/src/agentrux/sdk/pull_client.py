"""PullClient - cursor-based polling of GET /topics/{id}/events.

v0.3 wire change:
- Cursor is an `evt_<uuid>` string ONLY (no integer `after_sequence_no`).
- Server response is `{"events": [...], "next": {...}, "topic": {...}}`,
  surfaced as `ListEventsPage` by the low-level client.
- Adaptive interval: when the last poll returned events, drop to
  `min_interval_ms`; otherwise exponentially back off up to
  `max_interval_ms` (gives near-instant follow-up on a busy topic while
  not hammering an idle one).

`start_after_event_id=None` means "start from latest":
- Topic has events → cursor = head event_id (subscriber sees only
  strictly newer events).
- Topic is empty → use `since=now` until the first event arrives,
  then switch to cursor-based pagination on the next poll.

To replay from the earliest available event, set
`start_after_event_id=topic.oldest_available_evt_id` (the field returned
on every list response). Setting it manually keeps the explicit "I want
history" intent visible in the call site.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Awaitable, Callable

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.pull")


class PullClient:
    """Cursor-based event consumer with adaptive polling."""

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        poll_interval_ms: int = 1000,
        min_interval_ms: int = 100,
        max_interval_ms: int = 30_000,
        batch_size: int = 100,
        pipeline: MessagePipeline | None = None,
        adaptive_polling: bool = True,
        on_event: Callable[[MessageEnvelope], Awaitable[None]] | None = None,
        start_after_event_id: str | None = None,
    ) -> None:
        if not topic_id.startswith("top_"):
            raise ValueError(f"topic_id must start with 'top_', got {topic_id!r}")
        if start_after_event_id is not None and not start_after_event_id.startswith("evt_"):
            raise ValueError(
                f"start_after_event_id must start with 'evt_', "
                f"got {start_after_event_id!r}"
            )
        self._api = api_client
        self._topic_id = topic_id
        self._interval_ms = poll_interval_ms
        self._min_interval_ms = min_interval_ms
        self._max_interval_ms = max_interval_ms
        self._batch_size = batch_size
        self._pipeline = pipeline or MessagePipeline()
        self._pipeline.set_topic_id(topic_id)
        self._adaptive = adaptive_polling
        self._on_event = on_event
        # Cursor (evt_<uuid> string). None means "start from latest",
        # which is resolved on the first poll via _initialize_to_latest.
        self._cursor: str | None = start_after_event_id
        # True once we've resolved "latest" (only relevant when the
        # caller didn't supply start_after_event_id explicitly).
        self._latest_initialized: bool = start_after_event_id is not None
        # When the topic is empty at subscribe time, we filter
        # `since=now` until the first event arrives, then drop the
        # filter and switch to pure cursor-based paging.
        self._since_filter: str | None = None
        self._running = False
        self._stats = SDKStats(current_mode="pull")
        self._current_interval_ms = poll_interval_ms
        # External wake signal: an SSE hint can set this to skip the
        # sleep and poll immediately on the next iteration.
        self._wake_event = asyncio.Event()

    async def __aenter__(self) -> PullClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    def wake(self) -> None:
        """Skip the current poll-interval sleep and poll right away.

        Called by HybridConsumer when an SSE hint arrives — converts the
        push notification into a low-latency pull.
        """
        self._wake_event.set()

    @property
    def cursor(self) -> str | None:
        return self._cursor

    def restore_cursor(self, cursor: str) -> None:
        if not cursor.startswith("evt_"):
            raise ValueError(f"cursor must start with 'evt_', got {cursor!r}")
        self._cursor = cursor

    async def _initialize_to_latest(self) -> None:
        """Resolve "start from latest" by probing the topic head.

        Called once on the first poll when `start_after_event_id=None`.
        If the topic already has events, we capture the head event_id as
        our cursor so all subsequent polls see only strictly-newer
        events. If the topic is empty, we install a `since=<sampled
        before probe>` filter and let the next non-empty poll seed the
        cursor.

        Race-avoidance (Codex 2nd review #2): we sample `now` BEFORE
        sending the probe. If an event arrives between the probe
        response and our `since=` install, the next poll still catches
        it (since>=now_sampled covers events from that moment forward).
        """
        # Sample first, query second. UTC ISO with explicit offset
        # matches the server's accepted format (pipe_router.py:1175).
        candidate_since = datetime.now(timezone.utc).isoformat()
        try:
            head_page = await self._api.list_events(
                topic_id=self._topic_id, limit=1, order="desc",
            )
        except Exception as e:  # noqa: BLE001 - first poll must be resilient
            logger.warning(
                "latest-init probe failed on topic=%s: %s; will retry next cycle",
                self._topic_id,
                e,
            )
            self._stats.errors += 1
            return  # leave _latest_initialized False, retry on next poll
        if head_page.events:
            self._cursor = head_page.events[-1].event_id
        else:
            self._since_filter = candidate_since
        self._latest_initialized = True

    async def poll_once(self) -> list[MessageEnvelope]:
        """One poll cycle. Returns events delivered through the pipeline."""
        if not self._latest_initialized and self._cursor is None:
            await self._initialize_to_latest()
            if not self._latest_initialized:
                # Init still failed (network error); skip this cycle.
                self._adapt_interval(has_data=False)
                return []
        try:
            page = await self._api.list_events(
                topic_id=self._topic_id,
                after=self._cursor,
                limit=self._batch_size,
                order="asc",
                since=self._since_filter,
            )
        except Exception as e:  # noqa: BLE001 - poll loop must be resilient
            logger.warning("poll error on topic=%s: %s", self._topic_id, e)
            self._stats.errors += 1
            self._adapt_interval(has_data=False)
            return []

        events = page.events
        if not events:
            self._adapt_interval(has_data=False)
            return []

        self._adapt_interval(has_data=True)

        delivered: list[MessageEnvelope] = []
        for msg in events:
            self._stats.messages_received += 1
            processed = await self._pipeline.process(msg)
            for d_msg in processed:
                self._stats.messages_delivered += 1
                if self._on_event is not None:
                    await self._on_event(d_msg)
                delivered.append(d_msg)

        # Advance cursor to the last DELIVERED event (so we don't replay
        # what the pipeline already gave us; the pipeline's Deduplicator
        # would absorb it but advancing makes the next poll cheaper).
        if delivered:
            self._cursor = delivered[-1].event_id
        elif events:
            # Pipeline absorbed everything (all duplicates). Still
            # advance to the last raw event so we don't re-fetch them.
            self._cursor = events[-1].event_id
        # If we were in since-filter mode and now have a cursor, drop
        # the filter — cursor-based pagination is the canonical path.
        if self._cursor is not None and self._since_filter is not None:
            self._since_filter = None

        flushed = await self._pipeline.flush()
        for f_msg in flushed:
            self._stats.messages_delivered += 1
            if self._on_event is not None:
                await self._on_event(f_msg)
            delivered.append(f_msg)

        return delivered

    def _adapt_interval(self, *, has_data: bool) -> None:
        if not self._adaptive:
            self._current_interval_ms = self._interval_ms
            return
        if has_data:
            self._current_interval_ms = self._min_interval_ms
        else:
            self._current_interval_ms = min(
                max(self._current_interval_ms * 2, self._min_interval_ms),
                self._max_interval_ms,
            )

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        self._running = True
        while self._running:
            delivered = await self.poll_once()
            for msg in delivered:
                yield msg

            # Sleep, but wake early if wake() was called.
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._current_interval_ms / 1000,
                )
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake_event.clear()

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._wake_event.set()  # unblock any pending wait
        self._stats.current_mode = "disconnected"

    @property
    def stats(self) -> SDKStats:
        return self._stats
