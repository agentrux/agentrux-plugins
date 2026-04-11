"""FlowController - Backpressure mechanism for consumer pacing."""
from __future__ import annotations

import asyncio

from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.stats import FlowControllerStats


class FlowController:
    """Manages backpressure between ingestion and consumption."""

    def __init__(
        self,
        max_buffer_size: int = 1000,
        high_watermark: float = 0.8,
        low_watermark: float = 0.3,
    ):
        if not 0 < low_watermark < high_watermark <= 1.0:
            raise ValueError("watermarks must satisfy 0 < low < high <= 1.0")
        self._queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue(maxsize=max_buffer_size)
        self._max_size = max_buffer_size
        self._high_watermark = high_watermark
        self._low_watermark = low_watermark
        self._paused = False
        self._stats = FlowControllerStats()

    async def push(self, msg: MessageEnvelope) -> None:
        """Add message to buffer. Blocks if buffer is full."""
        await self._queue.put(msg)
        self._stats.messages_pushed += 1
        self._check_watermarks()

    async def pull(self) -> MessageEnvelope:
        """Get next message. Blocks if buffer is empty."""
        msg = await self._queue.get()
        self._stats.messages_pulled += 1
        self._check_watermarks()
        return msg

    def try_pull(self) -> MessageEnvelope | None:
        """Non-blocking pull. Returns None if empty."""
        try:
            msg = self._queue.get_nowait()
            self._stats.messages_pulled += 1
            self._check_watermarks()
            return msg
        except asyncio.QueueEmpty:
            return None

    def _check_watermarks(self) -> None:
        utilization = self._queue.qsize() / self._max_size if self._max_size > 0 else 0
        if not self._paused and utilization >= self._high_watermark:
            self._paused = True
            self._stats.pause_count += 1
        elif self._paused and utilization <= self._low_watermark:
            self._paused = False

    @property
    def should_pause_ingestion(self) -> bool:
        return self._paused

    @property
    def current_size(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> FlowControllerStats:
        self._stats.current_buffer_size = self._queue.qsize()
        self._stats.buffer_utilization = (
            self._queue.qsize() / self._max_size if self._max_size > 0 else 0
        )
        return self._stats
