"""Pull mode read — GET /events polling.

SSOT: docs/04_design/sdk/sdk_design.md §5-1, docs/04_design/messaging/read_flow.md
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx

from agentrux_sdk.errors import (
    AgenTruxError,
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    ResourceNotFoundError,
    ServerError,
    ValidationError,
)
from agentrux_sdk.models import Event

if TYPE_CHECKING:
    from agentrux_sdk.facade import AgenTruxClient


def _parse_event(raw: dict[str, Any]) -> Event:
    """server response の event item を SDK Event に変換.

    field 名は SSOT (read_flow.md §event item) に一致: stored_at / payload_object_id。
    """
    stored_at = raw["stored_at"]
    return Event(
        event_id=raw["event_id"],
        topic_id=raw["topic_id"],
        event_type=raw.get("event_type", "user.event"),
        sequence_number=int(raw["sequence_number"]),
        stored_at=(
            datetime.fromisoformat(stored_at.replace("Z", "+00:00"))
            if isinstance(stored_at, str)
            else stored_at
        ),
        payload=raw.get("payload"),
        payload_object_id=raw.get("payload_object_id"),
        metadata=raw.get("metadata"),
    )


def _extract_oldest_available(r: httpx.Response) -> str | None:
    """ttl_expired cursor 404 (pipe_router._ttl_expired_cursor_response) の
    detail.details.oldest_available_evt_id を取り出す。 取れなければ None。"""
    try:
        detail = r.json().get("detail") or {}
        return (detail.get("details") or {}).get("oldest_available_evt_id")
    except Exception:
        return None


def _map_read_error(status: int, body_text: str) -> Exception:
    if status == 401:
        # raw SSE stream open / 直 GET の 401。 retry 1 回後も 401 ならここで terminal 化。
        return AuthenticationError(f"unauthorized: {body_text}")
    if status == 403:
        return PermissionDeniedError(f"scope_mismatch: {body_text}")
    if status == 404:
        return ResourceNotFoundError(f"topic not found: {body_text}")
    if status == 422:
        return ValidationError(f"invalid read query: {body_text}")
    if status == 429:
        # 通常 pull/hydration は request_with_retry が 429 を Retry-After 付きで処理するが、
        # SSE stream open (raw stream、 retry 非経由) 等の直 429 はここに来る。
        return RateLimitError(f"rate limited: {body_text}")
    if status >= 500:
        # 5xx は transient。 ServerError (TemporaryError 派生) にして hybrid が Pull fallback /
        # read_sse が再接続できるようにする (汎用 AgenTruxError だと捕捉対象から漏れる)。
        return ServerError(f"server error {status}: {body_text}")
    return AgenTruxError(f"read failed with {status}: {body_text}")


async def read_pull(
    client: AgenTruxClient,
    *,
    topic_id: str,
    after: str | None = None,
    limit: int = 100,
    poll_interval_seconds: float = 1.0,
    stop_when_empty: bool = False,
    on_cursor_advance: Callable[[str | None], None] | None = None,
) -> AsyncIterator[Event]:
    """GET /topics/{top_id}/events?after=&limit= を loop で polling.

    Args:
      after: cursor (evt_<uuid>)、 None なら server 側 default (最古から)
      limit: 1..100
      poll_interval_seconds: has_more=False で sleep してから再試行
      stop_when_empty: True なら最初に has_more=False を見たら break (test 用)
      on_cursor_advance: 内部 cursor が前進するたびに呼ぶ hook (SSE resync で使用)。
        yield された event だけでなく ttl_expired cursor_advance / 空ページ後の継続 cursor も
        伝える。 0 件再同期でも呼び元 (read_sse) が reconnect cursor を最新化できるようにする。
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")
    if limit < 1 or limit > 100:
        raise ValidationError(f"limit must be 1..100 (got {limit})")

    cursor = after

    def _advance(new_cursor: str | None) -> None:
        nonlocal cursor
        cursor = new_cursor
        if on_cursor_advance is not None:
            on_cursor_advance(new_cursor)

    while True:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["after"] = cursor
        r = await client._request("GET", f"/topics/{topic_id}/events", params=params)
        if r.status_code == 404 and "ttl_expired" in r.text:
            # after cursor が TTL evict された (read_flow.md §9-C、 pipe_router cursor_advance)。
            # oldest_available があればそこへ前進して継続 (gap 分は回収不能、 at-least-once 内)。
            # raise すると hybrid/SSE resync が死ぬので、 evicted cursor は前進で吸収する。
            oldest = _extract_oldest_available(r)
            if oldest is not None and oldest != cursor:
                _advance(oldest)
                continue
            # 進める先が無い (topic 空化で oldest=null、 または oldest==失効 cursor) →
            # 失効 cursor を None 化して先頭/最新から再開し、 同一 evicted cursor の再送
            # (pull の 404 tight loop / SSE reconnect の無効 Last-Event-ID) を断つ。
            _advance(None)
            if stop_when_empty:
                return
            await asyncio.sleep(poll_interval_seconds)
            continue
        if r.status_code != 200:
            raise _map_read_error(r.status_code, r.text)
        body = r.json()
        # SSOT read_flow.md §envelope: {"events": [...], "next": {"after", "has_more", "url"}}
        events = body.get("events", [])
        for raw in events:
            yield _parse_event(raw)

        nxt = body.get("next") or {}
        # next.after が server 推奨の継続 cursor (asc order)。 無ければ末尾 event_id で代替。
        next_after = nxt.get("after")
        if next_after is not None:
            _advance(next_after)
        elif events:
            _advance(events[-1]["event_id"])

        has_more = bool(nxt.get("has_more"))
        if not has_more:
            if stop_when_empty:
                return
            await asyncio.sleep(poll_interval_seconds)
