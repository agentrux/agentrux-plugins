"""Pipeline — read → transform → publish chain.

SSOT: docs/04_design/sdk/sdk_design.md §6,
      docs/04_design/messaging/cluster_agnostic_ordering.md §2 / §3-3

invariant:
- at-least-once 配送 (重複あり得る、 transform で idempotency_key 継承推奨)
- checkpoint は処理成功 event の **opaque cursor** (created_at 内包、行存在非依存) を commit
  (event_id ではなく cursor を保存することで idle 後の偽 RETENTION_MISS を回避)
- 重複は `EventIdDedupe` (event_id の bounded set) で排除
- **順序非保証**: near-order best-effort のみ (pull_client が batch 内ソート)。
  厳密順序が要る業務は client app 側で独自管理 (2026-06-13 user 確定)
- **`RetentionMissError`**: resume 位置が retention 外 → run 中断 (re-replay は ops 判断)
- no-loss は server の watermark+cursor が保証 (client は gap 検出しない)
- source_topic と sink_topic が同じ場合の loop は caller 責務 (本 SDK は検出しない)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agentrux_sdk.checkpoint import CheckpointStore, InMemoryCheckpointStore
from agentrux_sdk.dedupe import EventIdDedupe
from agentrux_sdk.errors import ValidationError
from agentrux_sdk.models import Event, PublishResult

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
        dedupe: EventIdDedupe | None = None,
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
        self._dedupe: EventIdDedupe = dedupe or EventIdDedupe()
        self._mode = mode
        self._pull_limit = pull_limit
        self._pull_interval = pull_interval_seconds
        self._stopped = False

    async def run(self, *, max_events: int | None = None) -> int:
        """run loop. max_events 指定で N 件処理後 return (test 用)。

        Returns: 処理成功 (publish 完了) した event 数

        Raises:
          RetentionMissError: resume cursor が retention 外の場合 (run を中断)。
            re-replay は ops 判断。
        """
        processed = 0
        last_cursor = await self._cp.load(self._src)

        iterator = self._make_iter(last_cursor=last_cursor)
        try:
            async for evt in iterator:
                # at-least-once 重複排除
                if self._dedupe.is_duplicate(evt.event_id):
                    continue

                transformed = await self._transform(evt)
                if transformed is None:
                    # filter (transform 戻り None) → checkpoint だけ進める
                    await self._commit_cursor(evt)
                    continue

                await self._publish(transformed, source_evt=evt)
                await self._commit_cursor(evt)
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
    def _make_iter(self, *, last_cursor: str | None):
        if self._mode == "pull":
            return self._client.read_pull(
                topic_id=self._src,
                after=last_cursor,
                limit=self._pull_limit,
                poll_interval_seconds=self._pull_interval,
            )
        if self._mode == "sse":
            return self._client.read_sse(
                topic_id=self._src, last_event_id=last_cursor, auto_reconnect=True
            )
        return self._client.read_hybrid(
            topic_id=self._src,
            last_event_id=last_cursor,
            poll_interval_seconds=self._pull_interval,
            limit=self._pull_limit,
        )

    async def _commit_cursor(self, evt: Event) -> None:
        """処理成功 event の opaque cursor を checkpoint に commit する.

        cursor が空 (後方互換 server) の場合は event_id で代替。
        opaque cursor を保存することで idle 後の偽 RETENTION_MISS を避ける。
        """
        cursor_to_save = evt.cursor if evt.cursor else evt.event_id
        await self._cp.commit(self._src, cursor_to_save)

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
