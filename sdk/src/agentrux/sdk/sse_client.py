"""SSEClient - Real-time event streaming with auto-reconnect."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Awaitable, Callable

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.reconnect import ExponentialBackoff
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.sse")


class SSEClient:
    """SSE-based real-time event consumer with pipeline processing.

    Supports async with for automatic cleanup:
        async with SSEClient(...) as client:
            async for msg in client:
                process(msg)
    """

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        pipeline: MessagePipeline | None = None,
        reconnect_strategy: ExponentialBackoff | None = None,
        on_event: Callable[[MessageEnvelope], Awaitable[None]] | None = None,
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
        start_sequence: int | None = None,
    ):
        self._api = api_client
        self._topic_id = topic_id
        self._pipeline = pipeline or MessagePipeline()
        self._pipeline.set_topic_id(topic_id)
        self._reconnect = reconnect_strategy or ExponentialBackoff()
        self._on_event = on_event
        self._on_error = on_error
        self._connected = False
        # If resuming, set _last_seq so the first connect_sse() sends
        # Last-Event-ID = start_sequence - 1 → server resumes at start_sequence.
        self._last_seq: int | None = (
            start_sequence - 1 if start_sequence is not None else None
        )
        self._stats = SDKStats(current_mode="sse")
        self._stop_event = asyncio.Event()

    async def __aenter__(self) -> "SSEClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        """Iterate over ordered, deduplicated events."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._connected = True
                async for seq, data in self._api.connect_sse(
                    self._topic_id,
                    last_event_id=self._last_seq,
                ):
                    if self._stop_event.is_set():
                        return

                    self._stats.messages_received += 1

                    if seq is None:
                        logger.warning("SSE event missing sequence number")
                        continue

                    msg = MessageEnvelope.from_sse_event(data, seq)

                    # SSE delivers hint-only payloads (topic_id + latest_sequence_no)
                    # which don't have a valid event_id UUID. Skip UUID validation
                    # for hints — HybridConsumer fetches full events via Pull.
                    if msg.event_id and not msg.validate_event_id():
                        logger.warning("Invalid event_id: %s", msg.event_id)
                        continue

                    delivered = await self._pipeline.process(msg)
                    for d_msg in delivered:
                        self._last_seq = d_msg.sequence_no
                        self._stats.messages_delivered += 1
                        latency = (time.monotonic() - d_msg.received_at) * 1000
                        self._stats.avg_delivery_latency_ms = (
                            self._stats.avg_delivery_latency_ms * 0.9 + latency * 0.1
                        )
                        if self._on_event:
                            await self._on_event(d_msg)
                        yield d_msg

                    # Periodic flush
                    flushed = await self._pipeline.flush()
                    for f_msg in flushed:
                        self._last_seq = f_msg.sequence_no
                        self._stats.messages_delivered += 1
                        if self._on_event:
                            await self._on_event(f_msg)
                        yield f_msg

                    attempt = 0  # Reset on successful message

            except asyncio.CancelledError:
                return
            except Exception as e:
                self._connected = False
                self._stats.errors += 1
                logger.warning("SSE connection error: %s", e)

                if self._on_error:
                    await self._on_error(e)

                if not self._reconnect.should_retry(attempt):
                    logger.error("Max reconnection attempts reached")
                    return

                delay_ms = self._reconnect.next_delay(attempt)
                self._stats.reconnections += 1
                logger.info("Reconnecting in %.0fms (attempt %d)...", delay_ms, attempt + 1)
                await asyncio.sleep(delay_ms / 1000)
                attempt += 1

    async def disconnect(self) -> None:
        """Signal stop."""
        self._stop_event.set()
        self._connected = False
        self._stats.current_mode = "disconnected"

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_sequence(self) -> int | None:
        return self._last_seq

    @property
    def stats(self) -> SDKStats:
        return self._stats
