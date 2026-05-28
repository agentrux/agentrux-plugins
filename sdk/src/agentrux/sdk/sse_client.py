"""SSE mode read — GET /events/stream with Last-Event-ID replay.

SSOT: docs/04_design/sdk/sdk_design.md §5-2, ADR-0002 (at-least-once)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from agentrux.sdk.errors import (
    AgenTruxError,
    ValidationError,
)
from agentrux.sdk.models import Event
from agentrux.sdk.pull_client import _map_read_error, _parse_event

if TYPE_CHECKING:
    from agentrux.sdk.facade import AgentRuxClient


async def read_sse(
    client: AgentRuxClient,
    *,
    topic_id: str,
    last_event_id: str | None = None,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = 3,
) -> AsyncIterator[Event]:
    """GET /topics/{top_id}/events/stream を SSE で消費.

    Last-Event-ID header (HTTP 標準 SSE) で replay 開始位置を指示。
    auto_reconnect=True で disconnect 時に最新 last_id から再接続を試行。
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")

    current_last: str | None = last_event_id
    reconnect_attempts = 0
    while True:
        try:
            async for evt in _stream_once(client, topic_id=topic_id, last_id=current_last):
                yield evt
                current_last = evt.event_id
            # 正常終了 (server close) → reconnect 判定
            if not auto_reconnect:
                return
            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                return
        except (httpx.HTTPError, AgenTruxError):
            if not auto_reconnect:
                raise
            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                raise


async def _stream_once(
    client: AgentRuxClient,
    *,
    topic_id: str,
    last_id: str | None,
) -> AsyncIterator[Event]:
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if last_id is not None:
        headers["Last-Event-ID"] = last_id

    # auth header を付与 (facade._request は完了 response を返すので使えない、
    # ここでは raw stream)
    aat = await client.auth.get_access_token()
    headers["Authorization"] = f"Bearer {aat}"

    async with client.http._client.stream(
        "GET", f"/topics/{topic_id}/events/stream", headers=headers
    ) as r:
        if r.status_code != 200:
            body = await r.aread()
            raise _map_read_error(r.status_code, body.decode("utf-8", errors="replace"))
        async for evt in _iter_sse_events(r):
            yield evt


async def _iter_sse_events(r: httpx.Response) -> AsyncIterator[Event]:
    """SSE frame parser (data: ... per RFC, blank line で event 区切り)."""
    buf: list[str] = []
    async for line in r.aiter_lines():
        if line == "":
            if buf:
                raw = _parse_sse_frame(buf)
                if raw is not None:
                    yield _parse_event(raw)
                buf.clear()
            continue
        if line.startswith(":"):  # comment
            continue
        buf.append(line)


def _parse_sse_frame(lines: list[str]) -> dict | None:
    """SSE frame (data:JSON 1 行) を dict に."""
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return None
    payload = "\n".join(data_lines)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None
