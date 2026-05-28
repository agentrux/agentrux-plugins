"""Hybrid mode read — SSE primary + Pull fallback.

SSOT: docs/04_design/sdk/sdk_design.md §5-3
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from agentrux.sdk.errors import AgenTruxError, ValidationError
from agentrux.sdk.models import Event
from agentrux.sdk.pull_client import read_pull
from agentrux.sdk.sse_client import read_sse

if TYPE_CHECKING:
    from agentrux.sdk.facade import AgentRuxClient


async def read_hybrid(
    client: AgentRuxClient,
    *,
    topic_id: str,
    last_event_id: str | None = None,
    poll_interval_seconds: float = 1.0,
    limit: int = 100,
) -> AsyncIterator[Event]:
    """SSE 優先で消費。 SSE が err / disconnect で枯渇したら Pull に fallback.

    invariant:
      - last_event_id を継承し続け、 SSE → Pull → SSE のどの mode でも 1 度配信した event の
        重複は最小化 (at-least-once は server 側保証、 SDK は best-effort dedupe しない)
      - SSE が成功している間は Pull 経路は使わない
      - Pull に切替後、 has_more=False の周期で SSE 再接続を試行
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")

    current_last: str | None = last_event_id
    while True:
        try:
            async for evt in read_sse(
                client,
                topic_id=topic_id,
                last_event_id=current_last,
                auto_reconnect=False,
            ):
                yield evt
                current_last = evt.event_id
        except (httpx.HTTPError, AgenTruxError):
            # SSE 失敗 → Pull fallback
            async for evt in read_pull(
                client,
                topic_id=topic_id,
                after=current_last,
                limit=limit,
                poll_interval_seconds=poll_interval_seconds,
                stop_when_empty=True,
            ):
                yield evt
                current_last = evt.event_id
            # Pull で空になったら SSE 再接続を試行 (loop top へ)
            continue

        # SSE が clean 終了 (auto_reconnect=False) → Pull fallback で取りこぼし回収 → 再接続 loop
        async for evt in read_pull(
            client,
            topic_id=topic_id,
            after=current_last,
            limit=limit,
            poll_interval_seconds=poll_interval_seconds,
            stop_when_empty=True,
        ):
            yield evt
            current_last = evt.event_id
