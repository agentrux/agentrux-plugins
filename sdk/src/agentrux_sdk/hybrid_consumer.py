"""Hybrid mode read — SSE primary + Pull fallback.

SSOT: docs/04_design/sdk/sdk_design.md §5-3

SSE と Pull は同一 opaque cursor 形式を共有するため、mode 切替時に cursor を
そのまま受け渡せる (row 解決非依存、境界 gap なし)。
RetentionMissError は両 mode 共通 — reconnect では解決しないので caller に raise する。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from agentrux_sdk.errors import RetentionMissError, TemporaryError, ValidationError
from agentrux_sdk.models import Event
from agentrux_sdk.pull_client import read_pull
from agentrux_sdk.sse_client import read_sse

if TYPE_CHECKING:
    from agentrux_sdk.facade import AgentRuxClient


async def read_hybrid(
    client: AgentRuxClient,
    *,
    topic_id: str,
    last_event_id: str | None = None,
    poll_interval_seconds: float = 1.0,
    limit: int = 100,
) -> AsyncIterator[Event]:
    """SSE 優先で消費。 SSE が err / disconnect で枯渇したら Pull に fallback.

    cursor は opaque cursor 形式で SSE / Pull 双方で共有する。
    mode 切替時も同一 cursor を使うため境界 gap は生じない。

    invariant:
      - cursor を継承し続け、 SSE → Pull → SSE のどの mode でも 1 度配信した event の
        重複は最小化 (at-least-once は server 側保証、 SDK は per-event cursor で追従)
      - SSE が成功している間は Pull 経路は使わない
      - Pull に切替後、 has_more=False の周期で SSE 再接続を試行
      - **transient な失敗 (network / 5xx / 一過性) のみ Pull に fallback する。**
        permanent error (403 PermissionDenied / 404 ResourceNotFound / 401 Authentication /
        429 RateLimit 等) は Pull でも同じく失敗するので fallback せず caller に raise する。
      - RetentionMissError は SSE / Pull 共に raise → caller に伝播する。
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")

    current_cursor: str | None = last_event_id

    def _advance_cursor(cursor: str | None) -> None:
        # Pull fallback が 0 件再同期 (ttl_expired cursor_advance / oldest=null で None 化) でも
        # current_cursor を最新化し、 次の read_sse(last_event_id=) に evicted cursor を渡さない。
        nonlocal current_cursor
        current_cursor = cursor

    while True:
        try:
            async for evt in read_sse(
                client,
                topic_id=topic_id,
                last_event_id=current_cursor,
                auto_reconnect=False,
            ):
                yield evt
                # SSE は per-event opaque cursor を evt.cursor に持つ
                current_cursor = evt.cursor or evt.event_id
        except RetentionMissError:
            # retention_miss は Pull fallback でも解決しない → caller に伝播
            raise
        except (httpx.HTTPError, TemporaryError):
            # SSE が transient に失敗 → Pull fallback (permanent error はここで捕まえず raise)
            async for evt in read_pull(
                client,
                topic_id=topic_id,
                after=current_cursor,
                limit=limit,
                poll_interval_seconds=poll_interval_seconds,
                stop_when_empty=True,
                on_cursor_advance=_advance_cursor,
            ):
                yield evt
                current_cursor = evt.cursor or evt.event_id
            # Pull で空になったら SSE 再接続を試行 (loop top へ)
            continue

        # SSE が clean 終了 (auto_reconnect=False) → Pull fallback で取りこぼし回収 → 再接続 loop
        async for evt in read_pull(
            client,
            topic_id=topic_id,
            after=current_cursor,
            limit=limit,
            poll_interval_seconds=poll_interval_seconds,
            stop_when_empty=True,
            on_cursor_advance=_advance_cursor,
        ):
            yield evt
            current_cursor = evt.cursor or evt.event_id
