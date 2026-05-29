"""Pipeline — read → transform → publish chain.

SSOT: docs/04_design/sdk/sdk_design.md §6

invariant:
- at-least-once 配送 (重複あり得る、 transform で idempotency_key 継承推奨)
- checkpoint は処理成功 event のみ commit
- gap 検出時は GapDetectedError で run 中断 (re-replay は ops 判断)
- source_topic と sink_topic が同じ場合の loop は caller 責務 (本 SDK は検出しない)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agentrux_sdk.checkpoint import CheckpointStore, InMemoryCheckpointStore
from agentrux_sdk.errors import ValidationError
from agentrux_sdk.gap_detector import SequenceGapDetector
from agentrux_sdk.models import Event, PublishResult
from agentrux_sdk.reorder_buffer import ReorderBuffer

if TYPE_CHECKING:
    from agentrux_sdk.facade import AgentRuxClient

TransformFn = Callable[[Event], Awaitable[dict | bytes | None]]


class Pipeline:
    """read source → transform → publish sink を 1 process で実行."""

    def __init__(
        self,
        client: AgentRuxClient,
        *,
        source_topic: str,
        sink_topic: str,
        transform: TransformFn,
        checkpoint_store: CheckpointStore | None = None,
        gap_detector: SequenceGapDetector | None = None,
        reorder_buffer: ReorderBuffer | None = None,
        mode: str = "pull",  # "pull" | "sse" | "hybrid"
        pull_limit: int = 100,
        pull_interval_seconds: float = 1.0,
    ) -> None:
        if not source_topic.startswith("top_") or not sink_topic.startswith("top_"):
            raise ValidationError("source_topic and sink_topic must start with 'top_'")
        if mode not in ("pull", "sse", "hybrid"):
            raise ValidationError(f"mode must be 'pull'/'sse'/'hybrid' (got {mode!r})")

        self._client = client
        self._src = source_topic
        self._sink = sink_topic
        self._transform = transform
        self._cp: CheckpointStore = checkpoint_store or InMemoryCheckpointStore()
        self._gap: SequenceGapDetector | None = gap_detector
        self._buf: ReorderBuffer | None = reorder_buffer
        self._mode = mode
        self._pull_limit = pull_limit
        self._pull_interval = pull_interval_seconds
        self._stopped = False

    async def run(self, *, max_events: int | None = None) -> int:
        """run loop. max_events 指定で N 件処理後 return (test 用)。

        Returns: 処理成功 (publish 完了) した event 数
        """
        processed = 0
        last_cp = await self._cp.load(self._src)

        iterator = self._make_iter(last_event_id=last_cp)
        try:
            async for evt in iterator:
                if self._gap is not None:
                    self._gap.observe(evt.sequence_number, topic_id=self._src)

                events_to_emit = await self._maybe_reorder(evt)
                for in_order_evt in events_to_emit:
                    transformed = await self._transform(in_order_evt)
                    if transformed is None:
                        # filter (transform 戻り None) → checkpoint だけ進める
                        await self._cp.commit(self._src, in_order_evt.event_id)
                        continue
                    await self._publish(transformed, source_evt=in_order_evt)
                    await self._cp.commit(self._src, in_order_evt.event_id)
                    processed += 1
                    if max_events is not None and processed >= max_events:
                        self._stopped = True
                        return processed
                if self._stopped:
                    return processed
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

        return processed

    def stop(self) -> None:
        self._stopped = True

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _make_iter(self, *, last_event_id: str | None):
        if self._mode == "pull":
            return self._client.read_pull(
                topic_id=self._src,
                after=last_event_id,
                limit=self._pull_limit,
                poll_interval_seconds=self._pull_interval,
            )
        if self._mode == "sse":
            return self._client.read_sse(
                topic_id=self._src, last_event_id=last_event_id, auto_reconnect=True
            )
        return self._client.read_hybrid(
            topic_id=self._src,
            last_event_id=last_event_id,
            poll_interval_seconds=self._pull_interval,
            limit=self._pull_limit,
        )

    async def _maybe_reorder(self, evt: Event) -> list[Event]:
        if self._buf is None:
            return [evt]
        return await self._buf.push(sequence_number=evt.sequence_number, event=evt)

    async def _publish(self, transformed: Any, *, source_evt: Event) -> PublishResult:
        # transform 戻り値は dict / bytes (BaseModel は publish 側で受理)
        # idempotency_key を source event_id にして downstream の at-most-once を helper
        return await self._client.publish(
            topic_id=self._sink,
            payload=transformed,
            idempotency_key=f"idk_pipe_{source_evt.event_id}",
        )


# Backward-compat 関数 (5.2 skeleton)
async def run_pipeline() -> None:  # pragma: no cover
    raise NotImplementedError("use Pipeline class directly")
