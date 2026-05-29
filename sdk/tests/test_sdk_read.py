"""SDK Phase 5.5 — read 3 modes (Pull / SSE / Hybrid)."""

from __future__ import annotations

import httpx
import pytest

from agentrux_sdk import AgentRuxClient
from agentrux_sdk.errors import (
    AuthenticationError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)

pytestmark = pytest.mark.unit


def _make_client_with(handler: callable) -> AgentRuxClient:
    client = AgentRuxClient(
        endpoint="https://api.example.com",
        client_id="crd_r",
        client_secret="aks_r",
    )
    client._http._client = httpx.AsyncClient(
        base_url=client.config.endpoint,
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": client.config.user_agent},
    )
    return client


def _token_response(req: httpx.Request) -> httpx.Response | None:
    if req.url.path == "/oauth/token":
        return httpx.Response(
            200, json={"access_token": "aat_r", "token_type": "Bearer", "expires_in": 600}
        )
    return None


def _event(i: int) -> dict:
    # SSOT read_flow.md §event item: stored_at (occurred_at は server が emit しない)
    return {
        "event_id": f"evt_{i:08d}",
        "topic_id": "top_x",
        "event_type": "user.event",
        "sequence_number": i,
        "stored_at": "2026-05-17T00:00:00+00:00",
        "payload": {"i": i},
    }


def _page(events: list[dict], *, has_more: bool, after: str | None = None) -> dict:
    """SSOT read_flow.md §envelope: {"events": [...], "next": {"after", "has_more"}}."""
    return {"events": events, "next": {"after": after, "has_more": has_more}}


# ============================================================================
# Pull mode
# ============================================================================


@pytest.mark.asyncio
async def test_pull_yields_events_until_has_more_false() -> None:
    state = {"call": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        state["call"] += 1
        return httpx.Response(200, json=_page([_event(1), _event(2), _event(3)], has_more=False))

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_pull(topic_id="top_x", stop_when_empty=True):
            seen.append(evt.sequence_number)
        assert seen == [1, 2, 3]
        assert state["call"] == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_carries_cursor_on_next_page() -> None:
    pages = [
        _page([_event(1), _event(2)], has_more=True, after="evt_00000002"),
        _page([_event(3)], has_more=False),
    ]
    state = {"idx": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        # 2 回目以降は ?after=evt_00000002 が付くこと
        if state["idx"] == 1:
            assert "after" in req.url.params
            assert req.url.params["after"] == "evt_00000002"
        body = pages[state["idx"]]
        state["idx"] += 1
        return httpx.Response(200, json=body)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_pull(topic_id="top_x", stop_when_empty=True):
            seen.append(evt.sequence_number)
        assert seen == [1, 2, 3]
        assert state["idx"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_404_raises_resource_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    client = _make_client_with(handler)
    try:
        with pytest.raises(ResourceNotFoundError):
            async for _ in client.read_pull(topic_id="top_x", stop_when_empty=True):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_403_raises_permission_denied() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(403, json={"error": {"code": "FORBIDDEN"}})

    client = _make_client_with(handler)
    try:
        with pytest.raises(PermissionDeniedError):
            async for _ in client.read_pull(topic_id="top_x", stop_when_empty=True):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_ttl_expired_cursor_advances_to_oldest() -> None:
    """after cursor が TTL evict → 404 ttl_expired を raise せず oldest_available へ前進して継続."""
    state = {"call": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        state["call"] += 1
        if state["call"] == 1:
            # evicted cursor → pipe_router._ttl_expired_cursor_response 形 (FastAPI detail wrap)
            return httpx.Response(
                404,
                json={
                    "detail": {
                        "error": "NOT_FOUND",
                        "message": "after cursor refers to a TTL-expired event",
                        "details": {
                            "reason": "ttl_expired",
                            "oldest_available_evt_id": "evt_00000005",
                        },
                        "next_action": "cursor_advance",
                    }
                },
            )
        # advance 後の pull は oldest から再開
        assert req.url.params["after"] == "evt_00000005"
        return httpx.Response(200, json=_page([_event(6)], has_more=False))

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_pull(
            topic_id="top_x", after="evt_00000001", stop_when_empty=True
        ):
            seen.append(evt.sequence_number)
        assert seen == [6]
        assert state["call"] == 2
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_rejects_invalid_topic_prefix_client_side() -> None:
    client = _make_client_with(lambda req: httpx.Response(500))
    try:
        with pytest.raises(ValidationError, match="top_"):
            async for _ in client.read_pull(topic_id="invalid"):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pull_rejects_out_of_range_limit() -> None:
    client = _make_client_with(lambda req: httpx.Response(500))
    try:
        with pytest.raises(ValidationError, match="limit"):
            async for _ in client.read_pull(topic_id="top_x", limit=0):
                pass
    finally:
        await client.aclose()


# ============================================================================
# SSE mode (text/event-stream) — server は hint-only (read_flow.md §9-C-4)。
# SDK は hint ごとに GET /events/{evt_id} で本体を hydrate して full Event を yield。
# ============================================================================

_SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _hint_frame(event_obj: dict, *, frame_id: str | None = None) -> bytes:
    """`event: hint` frame (payload 本体は含まない、 SSOT §9-C-4)."""
    import json

    parts: list[str] = []
    if frame_id is not None:
        parts.append(f"id: {frame_id}")
    parts.append("event: hint")
    hint = {
        "topic_id": event_obj["topic_id"],
        "event_id": event_obj["event_id"],
        "seq": event_obj["sequence_number"],
        "event_type": event_obj["event_type"],
        "payload_kind": "inline",
        "stored_at": event_obj["stored_at"],
    }
    parts.append(f"data: {json.dumps(hint)}")
    return ("\n".join(parts) + "\n\n").encode()


def _named_frame(event_name: str, data_obj: dict) -> bytes:
    """`event: <name>` frame (error / resync_required 用)."""
    import json

    return (f"event: {event_name}\ndata: {json.dumps(data_obj)}\n\n").encode()


def _evt_id_to_seq(path: str) -> int:
    # /topics/top_x/events/evt_00000001 → 1
    return int(path.rsplit("/", 1)[-1].split("_")[-1])


@pytest.mark.asyncio
async def test_sse_hint_frames_hydrated_to_full_events() -> None:
    stream_body = (
        _hint_frame(_event(1), frame_id="evt_00000001")
        + _hint_frame(_event(2), frame_id="evt_00000002")
        + _hint_frame(_event(3), frame_id="evt_00000003")
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            return httpx.Response(200, content=stream_body, headers=_SSE_HEADERS)
        if "/events/" in path:  # hydration GET /events/{evt_id}
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", auto_reconnect=False):
            seen.append(evt.sequence_number)
        assert seen == [1, 2, 3]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_last_event_id_header_sent_on_replay() -> None:
    captured = {"last_id": None}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            captured["last_id"] = req.headers.get("last-event-id")
            return httpx.Response(
                200,
                content=_hint_frame(_event(100), frame_id="evt_00000100"),
                headers=_SSE_HEADERS,
            )
        if "/events/" in path:
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        async for _ in client.read_sse(
            topic_id="top_x", last_event_id="evt_00000099", auto_reconnect=False
        ):
            pass
        assert captured["last_id"] == "evt_00000099"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_open_401_force_refresh_retries_once() -> None:
    """SSE stream open が 401 → 1 回だけ force_refresh して再 open (request_with_auth と同契約)."""
    state = {"stream_open": 0, "token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            state["token"] += 1
            return httpx.Response(
                200, json={"access_token": "aat_r", "token_type": "Bearer", "expires_in": 600}
            )
        path = req.url.path
        if path.endswith("/events/stream"):
            state["stream_open"] += 1
            if state["stream_open"] == 1:
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(
                200,
                content=_hint_frame(_event(1), frame_id="evt_00000001"),
                headers=_SSE_HEADERS,
            )
        if "/events/" in path:
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", auto_reconnect=False):
            seen.append(evt.sequence_number)
        assert seen == [1]
        assert state["stream_open"] == 2  # 401 → reopen
        assert state["token"] >= 2  # 初回 get_access_token + force_refresh
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_stream_http_404_raises_resource_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    client = _make_client_with(handler)
    try:
        with pytest.raises(ResourceNotFoundError):
            async for _ in client.read_sse(topic_id="top_x", auto_reconnect=False):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_error_frame_maps_to_exception() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        if req.url.path.endswith("/events/stream"):
            return httpx.Response(
                200,
                content=_named_frame("error", {"code": "UNAUTHORIZED", "reason": "jwt_expired"}),
                headers=_SSE_HEADERS,
            )
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        with pytest.raises(AuthenticationError):
            async for _ in client.read_sse(topic_id="top_x", auto_reconnect=False):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_resync_required_falls_back_to_pull_inline() -> None:
    """resync_required は接続を維持したまま Pull 再同期 (例外化しない、 ADR-0002)."""
    stream_body = _hint_frame(_event(1), frame_id="evt_00000001") + _named_frame(
        "resync_required", {"reason": "retention_miss", "next_action": "pull_resync"}
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            return httpx.Response(200, content=stream_body, headers=_SSE_HEADERS)
        if path.endswith("/events"):  # pull resync list
            return httpx.Response(200, json=_page([_event(2), _event(3)], has_more=False))
        if "/events/" in path:  # hydration of hint #1
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", auto_reconnect=False):
            seen.append(evt.sequence_number)
        assert seen == [1, 2, 3]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_resync_zero_event_advances_reconnect_cursor() -> None:
    """resync の Pull が 0 件でも ttl_expired cursor_advance を reconnect cursor に伝播する."""
    last_ids: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            last_ids.append(req.headers.get("last-event-id"))
            n = len(last_ids)
            if n == 1:
                # evicted cursor で再開 → server は retention_miss resync を指示
                return httpx.Response(
                    200,
                    content=_named_frame(
                        "resync_required",
                        {"reason": "retention_miss", "next_action": "pull_resync"},
                    ),
                    headers=_SSE_HEADERS,
                )
            if n == 2:
                return httpx.Response(
                    200,
                    content=_hint_frame(_event(6), frame_id="evt_00000006"),
                    headers=_SSE_HEADERS,
                )
            return httpx.Response(200, content=b"", headers=_SSE_HEADERS)  # clean close
        if path.endswith("/events"):  # pull resync list
            after = req.url.params.get("after")
            if after == "evt_00000001":
                return httpx.Response(
                    404,
                    json={
                        "detail": {
                            "error": "NOT_FOUND",
                            "details": {
                                "reason": "ttl_expired",
                                "oldest_available_evt_id": "evt_00000005",
                            },
                            "next_action": "cursor_advance",
                        }
                    },
                )
            return httpx.Response(200, json=_page([], has_more=False))  # advance 後は空
        if "/events/" in path:  # hydration
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", last_event_id="evt_00000001"):
            seen.append(evt.sequence_number)
        assert seen == [6]
        # reconnect #2 の Last-Event-ID は evicted evt_00000001 ではなく前進後の evt_00000005
        assert last_ids[1] == "evt_00000005"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_resync_null_oldest_clears_reconnect_cursor() -> None:
    """oldest_available=null (topic 空化) の resync は失効 cursor を捨て、 reconnect で Last-Event-ID を送らない."""
    last_ids: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            last_ids.append(req.headers.get("last-event-id"))
            n = len(last_ids)
            if n == 1:
                return httpx.Response(
                    200,
                    content=_named_frame(
                        "resync_required",
                        {"reason": "retention_miss", "next_action": "pull_resync"},
                    ),
                    headers=_SSE_HEADERS,
                )
            if n == 2:
                return httpx.Response(
                    200,
                    content=_hint_frame(_event(9), frame_id="evt_00000009"),
                    headers=_SSE_HEADERS,
                )
            return httpx.Response(200, content=b"", headers=_SSE_HEADERS)
        if path.endswith("/events"):  # pull resync: 利用可能 event 皆無 → oldest=null
            return httpx.Response(
                404,
                json={
                    "detail": {
                        "error": "NOT_FOUND",
                        "details": {"reason": "ttl_expired", "oldest_available_evt_id": None},
                        "next_action": "cursor_advance",
                    }
                },
            )
        if "/events/" in path:
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", last_event_id="evt_00000001"):
            seen.append(evt.sequence_number)
        assert seen == [9]
        # 失効 cursor を None 化 → reconnect #2 は Last-Event-ID 不在 (server 初回接続扱い)
        assert last_ids[1] is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_hint_hydration_404_is_skipped() -> None:
    """hint hydration が 404 (TTL 失効) → skip して次に進む (raise しない)."""
    stream_body = _hint_frame(_event(1), frame_id="evt_00000001") + _hint_frame(
        _event(2), frame_id="evt_00000002"
    )

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            return httpx.Response(200, content=stream_body, headers=_SSE_HEADERS)
        if "/events/" in path:
            seq = _evt_id_to_seq(path)
            if seq == 1:
                return httpx.Response(
                    404, json={"error": {"code": "NOT_FOUND", "reason": "ttl_expired"}}
                )
            return httpx.Response(200, json=_event(seq))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_sse(topic_id="top_x", auto_reconnect=False):
            seen.append(evt.sequence_number)
        assert seen == [2]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_hint_hydration_non_ttl_404_raises() -> None:
    """ttl_expired 以外の 404 は silent loss を避けるため raise (skip しない)."""
    stream_body = _hint_frame(_event(1), frame_id="evt_00000001")

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            return httpx.Response(200, content=stream_body, headers=_SSE_HEADERS)
        if "/events/" in path:
            # ttl_expired ではない一般 404 (テナント隔離 / 別 Topic 等)
            return httpx.Response(404, json={"error": "NOT_FOUND", "message": "no such event"})
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        with pytest.raises(ResourceNotFoundError):
            async for _ in client.read_sse(topic_id="top_x", auto_reconnect=False):
                pass
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_sse_rejects_invalid_topic_prefix() -> None:
    client = _make_client_with(lambda req: httpx.Response(500))
    try:
        with pytest.raises(ValidationError):
            async for _ in client.read_sse(topic_id="invalid", auto_reconnect=False):
                pass
    finally:
        await client.aclose()


# ============================================================================
# Hybrid mode (SSE primary + Pull fallback)
# ============================================================================


@pytest.mark.asyncio
async def test_hybrid_falls_back_to_pull_on_transient_sse_failure() -> None:
    """SSE stream が 5xx (transient) → Pull fallback で event を回収する."""
    state = {"stream_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            state["stream_calls"] += 1
            return httpx.Response(503, text="upstream unavailable")
        if path.endswith("/events"):  # pull fallback list
            return httpx.Response(200, json=_page([_event(1), _event(2)], has_more=False))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_hybrid(topic_id="top_x"):
            seen.append(evt.sequence_number)
            if len(seen) == 2:
                break  # hybrid は無限 loop なので 2 件回収したら抜ける
        assert seen == [1, 2]
        assert state["stream_calls"] >= 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hybrid_propagates_terminal_sse_error_without_pull() -> None:
    """SSE が permanent error (403) → Pull fallback せず caller に raise する."""
    state = {"pull_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            return httpx.Response(403, json={"error": {"code": "FORBIDDEN"}})
        if path.endswith("/events"):
            state["pull_calls"] += 1
            return httpx.Response(200, json=_page([_event(1)], has_more=False))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        with pytest.raises(PermissionDeniedError):
            async for _ in client.read_hybrid(topic_id="top_x"):
                pass
        assert state["pull_calls"] == 0  # terminal error で Pull に落ちない
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hybrid_pull_fallback_clears_evicted_cursor_for_next_sse() -> None:
    """SSE clean close → Pull fallback が ttl_expired(oldest=null) を cursor=None に吸収し、

    その前進結果を read_hybrid の current_last に伝播 → 次の read_sse 再接続で evicted な
    Last-Event-ID を再送しない (round-4 fix: on_cursor_advance=_advance_last の検証)。
    """
    last_ids: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        path = req.url.path
        if path.endswith("/events/stream"):
            last_ids.append(req.headers.get("last-event-id"))
            n = len(last_ids)
            if n == 1:
                return httpx.Response(200, content=b"", headers=_SSE_HEADERS)  # clean close
            return httpx.Response(
                200,
                content=_hint_frame(_event(9), frame_id="evt_00000009"),
                headers=_SSE_HEADERS,
            )
        if path.endswith("/events"):  # pull fallback: 利用可能 event 皆無 → oldest=null
            return httpx.Response(
                404,
                json={
                    "detail": {
                        "error": "NOT_FOUND",
                        "details": {"reason": "ttl_expired", "oldest_available_evt_id": None},
                        "next_action": "cursor_advance",
                    }
                },
            )
        if "/events/" in path:  # hydration
            return httpx.Response(200, json=_event(_evt_id_to_seq(path)))
        return httpx.Response(500)

    client = _make_client_with(handler)
    try:
        seen = []
        async for evt in client.read_hybrid(topic_id="top_x", last_event_id="evt_00000001"):
            seen.append(evt.sequence_number)
            break  # hybrid は無限 loop なので 1 件回収したら抜ける
        assert seen == [9]
        assert last_ids[0] == "evt_00000001"  # 初回は呼び元指定の cursor
        # Pull が evicted cursor を None 化 → reconnect #2 は Last-Event-ID 不在
        assert last_ids[1] is None
    finally:
        await client.aclose()
