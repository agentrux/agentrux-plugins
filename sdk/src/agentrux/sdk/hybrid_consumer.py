"""HybridConsumer - SSE primary with Pull fallback."""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.pull_client import PullClient
from agentrux.sdk.reconnect import ExponentialBackoff
from agentrux.sdk.sse_client import SSEClient
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.hybrid")


class HybridConsumer:
    """SSE primary, Pull fallback consumer.

    1. Start with SSE
    2. On SSE failure -> switch to Pull mode
    3. On SSE recovery -> switch back to SSE
    4. Gap detection -> Pull API backfill
    """

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        pipeline: MessagePipeline | None = None,
        on_event: Callable[[MessageEnvelope], Awaitable[None]] | None = None,
        on_mode_change: Callable[[str], Awaitable[None]] | None = None,
        sse_reconnect: ExponentialBackoff | None = None,
        poll_interval_ms: int = 1000,
        start_sequence: int | None = None,
    ):
        self._api = api_client
        self._topic_id = topic_id
        self._pipeline = pipeline or MessagePipeline()
        self._pipeline.set_topic_id(topic_id)
        self._on_event = on_event
        self._on_mode_change = on_mode_change
        self._mode: str = "disconnected"
        self._running = False
        self._stats = SDKStats()

        self._sse = SSEClient(
            api_client=api_client,
            topic_id=topic_id,
            pipeline=self._pipeline,
            reconnect_strategy=sse_reconnect or ExponentialBackoff(max_retries=3),
            on_event=on_event,
            start_sequence=start_sequence,
        )
        self._pull = PullClient(
            api_client=api_client,
            topic_id=topic_id,
            pipeline=self._pipeline,
            poll_interval_ms=poll_interval_ms,
            on_event=on_event,
            start_sequence=start_sequence,
        )

    async def __aenter__(self) -> "HybridConsumer":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    async def _set_mode(self, mode: str) -> None:
        if mode != self._mode:
            old_mode = self._mode
            self._mode = mode
            self._stats.current_mode = mode
            logger.info("Mode changed: %s -> %s", old_mode, mode)
            if self._on_mode_change:
                await self._on_mode_change(mode)

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        """Yield messages, automatically switching between SSE and Pull."""
        self._running = True

        while self._running:
            # Try SSE first
            try:
                await self._set_mode("sse")
                sse_had_data = False
                async for msg in self._sse:
                    if not self._running:
                        return
                    sse_had_data = True
                    self._stats.messages_received += 1
                    self._stats.messages_delivered += 1
                    yield msg

                # SSE ended normally (max retries)
                if not sse_had_data:
                    logger.info("SSE yielded no data, falling back to Pull")

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("SSE failed, falling back to Pull: %s", e)
                self._stats.errors += 1

            if not self._running:
                return

            # Fallback to Pull
            try:
                await self._set_mode("pull")
                # Sync cursor from SSE
                if self._sse.last_sequence is not None:
                    pass  # Pipeline already tracks state

                pull_count = 0
                async for msg in self._pull:
                    if not self._running:
                        return
                    self._stats.messages_received += 1
                    self._stats.messages_delivered += 1
                    yield msg
                    pull_count += 1

                    # Periodically try to switch back to SSE
                    if pull_count % 100 == 0:
                        break  # Will retry SSE in outer loop

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Pull failed: %s", e)
                self._stats.errors += 1
                await asyncio.sleep(5)  # Wait before retry

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        await self._sse.disconnect()
        await self._pull.stop()
        await self._set_mode("disconnected")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def stats(self) -> SDKStats:
        return self._stats
