"""SSE mode read — GET /events/stream with Last-Event-ID replay.

SSOT: docs/04_design/sdk/sdk_design.md §5-2,
      docs/04_design/messaging/cluster_agnostic_ordering.md §3-3 / §3-5,
      ADR-0002 (at-least-once)

server SSE は hint-only (read_flow.md §9-C-4): `event: hint` frame は
{topic_id, event_id, cursor, event_type, payload_kind, stored_at} 等を含むが
payload 本体を含まない。SDK は sdk_design.md §5-2 の「read_sse は full Event を yield」
契約を守るため、hint を受けるたびに GET /events/{evt_id} で本体を hydrate して Event を yield する。

cursor (cluster_agnostic_ordering.md §3-3):
  - SSE frame の `id:` = per-event opaque cursor (Pull と同一形式)
  - reconnect 時は `Last-Event-ID` header に直近 opaque cursor を載せて継承
  - retention 外 → server は resync_required(retention_miss) → RetentionMissError raise
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import httpx

from agentrux_sdk.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitError,
    ResourceNotFoundError,
    RetentionMissError,
    ServerError,
    ValidationError,
)
from agentrux_sdk.models import Event
from agentrux_sdk.pull_client import _map_read_error, _parse_event, read_pull

if TYPE_CHECKING:
    from agentrux_sdk.facade import AgenTruxClient


class _Cursor:
    """SSE `id:` 行を権威とする再接続/再同期用 cursor (mutable holder).

    値は per-event opaque cursor (cluster_agnostic_ordering.md §3-3)。
    reconnect 時に Last-Event-ID header として使う。
    """

    __slots__ = ("value",)

    def __init__(self, value: str | None) -> None:
        self.value = value

    def set(self, value: str | None) -> None:
        """read_pull の on_cursor_advance hook 用 (前進した cursor を holder に反映)。

        value=None は「失効 cursor を捨てて先頭/最新から再開」 (oldest 不在の ttl_expired)。
        次 reconnect で Last-Event-ID を送らず、 server 初回接続扱い (現在 seq から) になる。
        """
        self.value = value


async def read_sse(
    client: AgenTruxClient,
    *,
    topic_id: str,
    last_event_id: str | None = None,
    auto_reconnect: bool = True,
    max_reconnect_attempts: int = 3,
) -> AsyncIterator[Event]:
    """GET /topics/{top_id}/events/stream を SSE で消費し full Event を yield.

    server は hint frame しか送らないので、hint ごとに GET /events/{evt_id} で本体を
    hydrate する。`event: error` は SDK 例外に map して raise、`event: resync_required`
    は retention_miss かどうかを確認:
      - retention_miss: RetentionMissError を raise (run を中断、 re-replay は ops 判断)
      - その他 (replay_gap 等): 接続を維持したまま Pull で再同期 (ADR-0002)

    Last-Event-ID header (HTTP 標準 SSE) で replay 開始位置を指示。cursor は受信した
    SSE `id:` を権威とし、hydration の成否に依存しない。
    auto_reconnect=True で disconnect / clean close 時に最新 cursor から再接続を試行。
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")

    cursor = _Cursor(last_event_id)
    reconnect_attempts = 0
    while True:
        try:
            async for evt in _stream_once(client, topic_id=topic_id, cursor=cursor):
                yield evt
            # 正常終了 (server close) → reconnect 判定
            if not auto_reconnect:
                return
            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                return
        except RetentionMissError:
            # retention_miss は reconnect では解決しない — run を中断
            raise
        except (httpx.HTTPError, AuthenticationError, ServerError) as exc:
            if not auto_reconnect:
                raise
            reconnect_attempts += 1
            if reconnect_attempts > max_reconnect_attempts:
                raise exc


async def _stream_once(
    client: AgenTruxClient,
    *,
    topic_id: str,
    cursor: _Cursor,
) -> AsyncIterator[Event]:
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if cursor.value is not None:
        headers["Last-Event-ID"] = cursor.value

    # auth header を付与 (facade._request は完了 response を返すので raw stream には使えない)。
    # request_with_auth と同じ 401 fallback を再現する: open が 401 なら 1 回だけ force_refresh
    # して再 open。 force_refresh は invalid_client を CredentialRotatedError、 それ以外 401 を
    # AuthenticationError に map するので、 ここでは判定不要。
    aat = await client.auth.get_access_token()
    headers["Authorization"] = f"Bearer {aat}"

    refreshed = False
    while True:
        async with client.http._client.stream(
            "GET", f"/topics/{topic_id}/events/stream", headers=headers
        ) as r:
            if r.status_code != 200:
                body = await r.aread()
                if r.status_code == 401 and not refreshed:
                    refreshed = True
                    aat = await client.auth.force_refresh()
                    headers["Authorization"] = f"Bearer {aat}"
                    continue
                raise _map_read_error(r.status_code, body.decode("utf-8", errors="replace"))
            async for event_type, data, frame_id in _iter_sse_frames(r):
                if event_type == "hint":
                    event_id = data.get("event_id")
                    if not event_id:
                        continue
                    # cursor は hint の `id:` フィールド (opaque cursor) を権威として前進。
                    # hint の event_id は hydration キーとしてのみ使用。
                    # error/heartbeat 等 非 hint frame の id: で汚染しない (再接続の
                    # Last-Event-ID が壊れるのを防ぐ)。hydration の成否には依存しない。
                    cursor.value = frame_id or event_id
                    evt = await _hydrate(client, topic_id=topic_id, event_id=event_id)
                    if evt is not None:
                        yield evt
                elif event_type == "error":
                    raise _map_sse_error(data)
                elif event_type == "resync_required":
                    # ADR-0002: resync_required frame の reason を確認
                    reason = data.get("reason", "")
                    if reason == "retention_miss":
                        # retention 外の resume は Pull resync でも解決しない → 中断
                        raise RetentionMissError(
                            f"SSE resync_required(retention_miss) for topic {topic_id!r}; "
                            "resume cursor is outside retention window; re-replay required",
                            topic_id=topic_id,
                        )
                    # replay_gap 等: 接続を維持したまま Pull で再同期。
                    # 未知の next_action は frame を無視して接続維持 (投機的処理をしない)。
                    if data.get("next_action") not in (None, "pull_resync"):
                        continue
                    # resume cursor は cursor.value (= Last-Event-ID と一致)。
                    # on_cursor_advance=cursor.set で 0 件再同期でも reconnect cursor を最新化。
                    async for evt in read_pull(
                        client,
                        topic_id=topic_id,
                        after=cursor.value,
                        limit=100,
                        stop_when_empty=True,
                        on_cursor_advance=cursor.set,
                    ):
                        yield evt
                        cursor.value = evt.cursor or evt.event_id
                # それ以外 (message / 未知) は無視
            return


async def _hydrate(client: AgenTruxClient, *, topic_id: str, event_id: str) -> Event | None:
    """hint の event_id で本体を取得。

    404 は **TTL 失効 (reason=ttl_expired) のときだけ** skip して None を返す。それ以外の 404
    (テナント隔離 A4 / 別 Topic 等の resource エラー) は silent loss を避けるため raise する。
    """
    r = await client._request("GET", f"/topics/{topic_id}/events/{event_id}")
    if r.status_code == 404:
        # SSOT pipe_router._ttl_expired_event_response: details.reason="ttl_expired" +
        # next_action="cursor_advance"。 TTL eviction は回収不能なので cursor を進めて継続。
        if "ttl_expired" in r.text:
            return None
        raise _map_read_error(r.status_code, r.text)
    if r.status_code != 200:
        raise _map_read_error(r.status_code, r.text)
    return _parse_event(r.json())


def _map_sse_error(data: dict[str, Any]) -> Exception:
    """`event: error` frame ({"code","reason"}) を SDK 例外に map."""
    code = (data or {}).get("code")
    reason = (data or {}).get("reason", "")
    msg = f"{code}: {reason}" if code else (reason or "sse error")
    if code == "UNAUTHORIZED":
        return AuthenticationError(msg)
    if code in ("FORBIDDEN", "SUSPENDED"):
        return PermissionDeniedError(msg)
    if code == "NOT_FOUND":
        return ResourceNotFoundError(msg)
    if code == "RATE_LIMITED":
        return RateLimitError(msg)
    return ServerError(msg)


async def _iter_sse_frames(
    r: httpx.Response,
) -> AsyncIterator[tuple[str, dict[str, Any], str | None]]:
    """SSE frame parser. (event_type, data_dict, frame_id) を yield.

    - 空行で frame 終端、`:` 始まりは comment (heartbeat) で無視。
    - `data:` は SSOT 上 JSON 1 行だが、SSE 仕様準拠で複数行 data は LF 結合する。
    - `event:` 省略時は SSE 仕様 default の "message"。
    - frame_id は opaque cursor (cluster_agnostic_ordering.md §3-3)。
    """
    event_type: str | None = None
    data_lines: list[str] = []
    frame_id: str | None = None

    async for raw_line in r.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines or event_type is not None:
                yield (event_type or "message", _decode_data(data_lines), frame_id)
            event_type, data_lines, frame_id = None, [], None
            continue
        if line.startswith(":"):  # comment / heartbeat
            continue
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("id:"):
            frame_id = line[len("id:") :].strip() or None
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
        # 未知の field 行は無視

    # blank line 終端の無い trailing frame (防御的)
    if data_lines or event_type is not None:
        yield (event_type or "message", _decode_data(data_lines), frame_id)


def _decode_data(data_lines: list[str]) -> dict[str, Any]:
    if not data_lines:
        return {}
    try:
        parsed = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
