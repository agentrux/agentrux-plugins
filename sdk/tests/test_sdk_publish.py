"""SDK Phase 5.4 — publish (inline / object_ref 自動切替 + error mapping)."""

from __future__ import annotations

import json

import httpx
import pytest
from agentrux_sdk import AgentRuxClient
from agentrux_sdk.errors import (
    AgenTruxError,
    ConflictError,
    IdempotencyConflictError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)
from agentrux_sdk.publish import INLINE_MAX_BYTES

pytestmark = pytest.mark.unit


def _make_client_with(handler: callable) -> AgentRuxClient:
    client = AgentRuxClient(
        endpoint="https://api.example.com",
        client_id="crd_pub",
        client_secret="aks_pub",
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
            200, json={"access_token": "aat_pub", "token_type": "Bearer", "expires_in": 600}
        )
    return None


# ============================================================================
# inline publish (<= 256 KiB)
# ============================================================================


@pytest.mark.asyncio
async def test_publish_inline_dict_payload() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        captured["headers"] = dict(req.headers)
        return httpx.Response(
            201,
            json={"event_id": "evt_abc"},
        )

    client = _make_client_with(handler)
    try:
        r = await client.publish(topic_id="top_x", payload={"foo": "bar"}, event_type="test.event")
        assert r.event_id == "evt_abc"
        assert r.idempotent_replayed is False
        assert captured["path"] == "/topics/top_x/events"
        assert captured["body"]["payload"] == {"foo": "bar"}
        assert captured["body"]["event_type"] == "test.event"
        assert "idempotency-key" in captured["headers"]
        assert captured["headers"]["idempotency-key"].startswith("idk_")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_inline_with_user_idempotency_key() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        captured["idk"] = req.headers["idempotency-key"]
        return httpx.Response(201, json={"event_id": "evt_y"})

    client = _make_client_with(handler)
    try:
        await client.publish(
            topic_id="top_x", payload={"k": 1}, idempotency_key="idk_user_supplied"
        )
        assert captured["idk"] == "idk_user_supplied"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_inline_idempotent_replayed_header() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(
            200,
            json={"event_id": "evt_replay"},
            headers={"Idempotent-Replayed": "true"},
        )

    client = _make_client_with(handler)
    try:
        r = await client.publish(topic_id="top_x", payload={"k": 2})
        assert r.idempotent_replayed is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_small_binary_routes_to_object_ref() -> None:
    """小さい binary (非 JSON) は inline ではなく object_ref 経路に振り分ける.

    server の inline payload は任意 JSON 値のみで binary 非対応のため、
    256 KiB 以下でも非 JSON bytes は presigned PUT 経路を通す。
    """
    state = {"payloads_post": 0, "events_post": 0, "put": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        if req.url.path == "/topics/top_x/payloads":
            state["payloads_post"] += 1
            return httpx.Response(
                201,
                json={
                    "payload_object_id": "pob_small",
                    "presigned_put_url": "https://put.example.com/pob_small",
                    "required_headers": {},
                },
            )
        if req.url.path == "/topics/top_x/events":
            state["events_post"] += 1
            body = json.loads(req.content)
            assert body == {"payload_object_id": "pob_small"}
            return httpx.Response(201, json={"event_id": "evt_sm"})
        return httpx.Response(500, text=f"unexpected: {req.url.path}")

    class _DirectMock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def put(self, url, content, headers):
            state["put"] += 1
            assert content == b"\x00\x01\x02binary-not-json"
            return httpx.Response(204)

    import agentrux_sdk.publish as pub_mod

    monkeypatch_factory = lambda *a, **kw: _DirectMock()  # noqa: E731

    client = _make_client_with(handler)
    pub_mod._DirectPUTClient = monkeypatch_factory  # type: ignore[assignment]
    try:
        r = await client.publish(topic_id="top_x", payload=b"\x00\x01\x02binary-not-json")
        assert r.event_id == "evt_sm"
        assert state["payloads_post"] == 1
        assert state["put"] == 1
        assert state["events_post"] == 1
    finally:
        pub_mod._DirectPUTClient = httpx.AsyncClient  # type: ignore[assignment]
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_brace_prefixed_binary_routes_to_object_ref() -> None:
    """先頭が '{' でも bytes は object_ref。 中身を sniff せず元の型で判定する.

    旧実装は raw 先頭文字で inline 判定したため、 b'{...not-json' が inline に流れて
    json.loads で落ちた。 routing は payload 型 (bytes → object_ref) で決める。
    """
    state = {"payloads_post": 0, "events_post": 0, "put": 0}
    not_json = b"{this-looks-like-json-but-is-not"

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        if req.url.path == "/topics/top_x/payloads":
            state["payloads_post"] += 1
            return httpx.Response(
                201,
                json={
                    "payload_object_id": "pob_brace",
                    "presigned_put_url": "https://put.example.com/pob_brace",
                    "required_headers": {},
                },
            )
        if req.url.path == "/topics/top_x/events":
            state["events_post"] += 1
            assert json.loads(req.content) == {"payload_object_id": "pob_brace"}
            return httpx.Response(201, json={"event_id": "evt_br"})
        return httpx.Response(500, text=f"unexpected: {req.url.path}")

    class _DirectMock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def put(self, url, content, headers):
            state["put"] += 1
            assert content == not_json
            return httpx.Response(204)

    import agentrux_sdk.publish as pub_mod

    client = _make_client_with(handler)
    pub_mod._DirectPUTClient = lambda *a, **kw: _DirectMock()  # type: ignore[assignment]
    try:
        r = await client.publish(topic_id="top_x", payload=not_json)
        assert r.event_id == "evt_br"
        assert state["payloads_post"] == 1
        assert state["put"] == 1
        assert state["events_post"] == 1
    finally:
        pub_mod._DirectPUTClient = httpx.AsyncClient  # type: ignore[assignment]
        await client.aclose()


# ============================================================================
# object_ref (size > 256 KiB)
# ============================================================================


@pytest.mark.asyncio
async def test_publish_object_ref_when_payload_exceeds_inline_max(monkeypatch) -> None:
    """large payload で presigned PUT → commit 3 step が起こる."""
    state = {"payloads_post": 0, "put": 0, "events_post": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        if req.url.path == "/topics/top_x/payloads":
            state["payloads_post"] += 1
            body = json.loads(req.content)
            assert body["size_bytes"] == INLINE_MAX_BYTES + 100
            # SSOT: presigned response key は presigned_put_url + required_headers
            return httpx.Response(
                201,
                json={
                    "payload_object_id": "pob_big",
                    "presigned_put_url": "https://put.example.com/upload/pob_big",
                    "required_headers": {"Content-Type": "application/octet-stream"},
                },
            )
        if req.url.path == "/topics/top_x/events":
            state["events_post"] += 1
            body = json.loads(req.content)
            # SSOT PublishEventBody: object_ref 経路の field 名は payload_object_id
            assert body == {"payload_object_id": "pob_big"}
            return httpx.Response(201, json={"event_id": "evt_big"})
        return httpx.Response(500, text=f"unexpected: {req.url.path}")

    # Patch the direct PUT to MinIO/S3 (presigned URL is outside our test transport).
    # publish.py 内の `async with httpx.AsyncClient(...)` のみ差し替えるため、
    # publish module の httpx 参照を限定 patch する。
    class _DirectMock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def put(self, url, content, headers):
            state["put"] += 1
            assert url == "https://put.example.com/upload/pob_big"
            assert len(content) == INLINE_MAX_BYTES + 100
            # required_headers が presigned PUT に転送されること
            assert headers.get("Content-Type") == "application/octet-stream"
            return httpx.Response(204)

        async def aclose(self):  # facade.aclose() 連鎖の保険 (実際には呼ばれない)
            return None

    import agentrux_sdk.publish as pub_mod

    def _factory(*a, **kw):
        return _DirectMock()

    monkeypatch.setattr(pub_mod, "_DirectPUTClient", _factory)

    client = _make_client_with(handler)
    try:
        big = b"x" * (INLINE_MAX_BYTES + 100)
        r = await client.publish(topic_id="top_x", payload=big)
        assert r.event_id == "evt_big"
        assert state["payloads_post"] == 1
        assert state["put"] == 1
        assert state["events_post"] == 1
    finally:
        await client.aclose()


# ============================================================================
# error mapping
# ============================================================================


@pytest.mark.asyncio
async def test_publish_403_permission_denied() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(
            403, json={"error": {"code": "FORBIDDEN", "message": "scope mismatch"}}
        )

    client = _make_client_with(handler)
    try:
        with pytest.raises(PermissionDeniedError):
            await client.publish(topic_id="top_x", payload={"k": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_404_resource_not_found() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "topic gone"}})

    client = _make_client_with(handler)
    try:
        with pytest.raises(ResourceNotFoundError):
            await client.publish(topic_id="top_x", payload={"k": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_409_idempotency_conflict() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(
            409,
            json={"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "body mismatch"}},
        )

    client = _make_client_with(handler)
    try:
        with pytest.raises(IdempotencyConflictError):
            await client.publish(topic_id="top_x", payload={"k": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_409_generic_conflict() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(
            409, json={"error": {"code": "CONFLICT", "message": "other conflict"}}
        )

    client = _make_client_with(handler)
    try:
        with pytest.raises(ConflictError) as ei:
            await client.publish(topic_id="top_x", payload={"k": 1})
        # idempotency variant でないことを確認
        assert not isinstance(ei.value, IdempotencyConflictError)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_422_validation_error_from_server() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(422, json={"error": {"code": "INVALID", "message": "bad schema"}})

    client = _make_client_with(handler)
    try:
        with pytest.raises(ValidationError):
            await client.publish(topic_id="top_x", payload={"k": 1})
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_invalid_topic_prefix_raises_client_side() -> None:
    """server に到達せず client-side で reject."""
    captured = {"calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["calls"] += 1
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(500)  # ここに来てはいけない

    client = _make_client_with(handler)
    try:
        with pytest.raises(ValidationError, match="top_"):
            await client.publish(topic_id="invalid-topic", payload={"k": 1})
        assert captured["calls"] == 0  # /oauth/token も呼ばない
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_unsupported_payload_type_raises_validation_error() -> None:
    client = _make_client_with(lambda req: httpx.Response(500))
    try:
        # set は JSON 値でも bytes でも BaseModel でもない → reject
        with pytest.raises(ValidationError, match="unsupported payload"):
            await client.publish(topic_id="top_x", payload={1, 2, 3})  # type: ignore[arg-type]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    ["hello", 12345, 3.14, True, None],
)
async def test_publish_scalar_json_routes_inline(payload: object) -> None:
    """scalar JSON (str/int/float/bool/None) は inline で payload field に載る (publish_flow any JSON)."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(201, json={"event_id": "evt_s"})

    client = _make_client_with(handler)
    try:
        await client.publish(topic_id="top_x", payload=payload)
        assert captured["path"] == "/topics/top_x/events"  # presigned 経路でない
        assert captured["body"]["payload"] == payload
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_publish_unexpected_5xx_after_retry_raises_temporary() -> None:
    """500 (non-retry 対象でない) でも payload 系 error は AgenTruxError generic."""

    def handler(req: httpx.Request) -> httpx.Response:
        if (r := _token_response(req)) is not None:
            return r
        return httpx.Response(500, text="server boom")

    client = _make_client_with(handler)
    try:
        with pytest.raises(AgenTruxError, match="publish failed"):
            await client.publish(topic_id="top_x", payload={"k": 1})
    finally:
        await client.aclose()
