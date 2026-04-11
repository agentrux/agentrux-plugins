"""PullClient - Cursor-based polling with adaptive intervals."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.pull")


class PullClient:
    """Cursor-based polling client with adaptive polling intervals."""

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        poll_interval_ms: int = 1000,
        min_interval_ms: int = 100,
        max_interval_ms: int = 30_000,
        batch_size: int = 50,
        pipeline: MessagePipeline | None = None,
        adaptive_polling: bool = True,
        on_event: Callable[[MessageEnvelope], Awaitable[None]] | None = None,
        start_sequence: int | None = None,
    ):
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
        self._cursor: str | None = None
        # Resume from a specific sequence: the first poll uses
        # after_sequence_no=start_sequence-1 so the server returns
        # events starting at start_sequence.
        self._after_sequence_no: int | None = (
            start_sequence - 1 if start_sequence is not None else None
        )
        self._running = False
        self._stats = SDKStats(current_mode="pull")
        self._current_interval_ms = poll_interval_ms

    async def __aenter__(self) -> "PullClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def poll_once(self) -> list[MessageEnvelope]:
        """Execute a single poll cycle. Returns delivered messages."""
        try:
            # On the first call after a resume, use after_sequence_no.
            # Once we have a cursor, switch to cursor-based pagination.
            if self._cursor is None and self._after_sequence_no is not None:
                items, next_cursor = await self._api.list_events(
                    topic_id=self._topic_id,
                    after_sequence_no=self._after_sequence_no,
                    limit=self._batch_size,
                )
                # Clear after_sequence_no after first use; subsequent polls
                # use the cursor (or after_sequence_no of the last delivered seq)
                self._after_sequence_no = None
            else:
                items, next_cursor = await self._api.list_events(
                    topic_id=self._topic_id,
                    cursor=self._cursor,
                    limit=self._batch_size,
                )
        except Exception as e:
            logger.warning("Poll error: %s", e)
            self._stats.errors += 1
            self._adapt_interval(has_data=False)
            return []

        if not items:
            self._adapt_interval(has_data=False)
            return []

        self._adapt_interval(has_data=True)
        if next_cursor:
            self._cursor = next_cursor

        delivered: list[MessageEnvelope] = []
        for item in items:
            self._stats.messages_received += 1
            try:
                msg = MessageEnvelope.from_api_response(item)
            except Exception as e:
                logger.warning("Invalid message: %s", e)
                continue

            if not msg.validate_event_id():
                continue

            processed = await self._pipeline.process(msg)
            for d_msg in processed:
                self._stats.messages_delivered += 1
                if self._on_event:
                    await self._on_event(d_msg)
                delivered.append(d_msg)

        # Update cursor to last delivered event
        if delivered:
            self._cursor = delivered[-1].event_id

        # Flush timed-out messages
        flushed = await self._pipeline.flush()
        for f_msg in flushed:
            self._stats.messages_delivered += 1
            if self._on_event:
                await self._on_event(f_msg)
            delivered.append(f_msg)

        return delivered

    def _adapt_interval(self, has_data: bool) -> None:
        if not self._adaptive:
            return
        if has_data:
            self._current_interval_ms = self._min_interval_ms
        else:
            self._current_interval_ms = min(
                self._current_interval_ms * 2,
                self._max_interval_ms,
            )

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        """Continuously poll and yield messages."""
        self._running = True
        while self._running:
            delivered = await self.poll_once()
            for msg in delivered:
                yield msg
            await asyncio.sleep(self._current_interval_ms / 1000)

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._stats.current_mode = "disconnected"

    def save_cursor(self) -> str | None:
        return self._cursor

    def restore_cursor(self, cursor: str) -> None:
        self._cursor = cursor

    @property
    def stats(self) -> SDKStats:
        return self._stats
