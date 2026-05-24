"""Tests for AgenTruxAPIClient — Topic data plane methods.

Covers publish / get / list / payload methods plus the shared retry /
401-refresh / 5xx-backoff plumbing in `_request_pipe`.

All HTTP is mocked via httpx.MockTransport (see conftest.make_api_client).
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from agentrux.sdk.client import OAuthRefreshTokenRefresher, TokenManager
from agentrux.sdk.envelope import ListEventsPage, MessageEnvelope, PublishResult
from agentrux.sdk.errors import (
    APIError,
    ForbiddenError,
    IdempotencyConflictError,
    InternalServerError,
    InvalidRequestError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    SDKError,
    SuspendedError,
    TTLExpiredError,
    UnauthorizedError,
)

from .conftest import (
    stub_event_view,
    stub_list_events_response,
    stub_pipe_error,
    stub_publish_response,
)


pytestmark = pytest.mark.asyncio


TOPIC = "top_00000000-0000-0000-0000-000000000001"
EVT = "evt_00000000-0000-0000-0000-000000000001"
EVT2 = "evt_00000000-0000-0000-0000-000000000002"
POB = "pob_00000000-0000-0000-0000-000000000001"


# ---------- publish_event ---------------------------------------------------


async def test_publish_normal_inline(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.read().decode()
        assert '"event_type"' in body
        assert '"payload"' in body
        return httpx.Response(200, json=stub_publish_response())

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        r = await api.publish_event(
            topic_id=TOPIC, event_type="hello.world", payload={"msg": "hi"}
        )
        assert isinstance(r, PublishResult)
        assert r.event_id.startswith("evt_")


async def test_publish_normal_object_ref(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_publish_response(
                payload_kind="object_ref",
                inline_size_bytes=None,
                payload_object_id=POB,
                size_bytes=2048,
            ),
        )

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        r = await api.publish_event(topic_id=TOPIC, payload_object_id=POB)
        assert r.payload_kind == "object_ref"


async def test_publish_with_idempotency_key_header(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["idempotency"] = req.headers.get("Idempotency-Key", "")
        return httpx.Response(200, json=stub_publish_response())

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        await api.publish_event(
            topic_id=TOPIC, payload={"x": 1}, idempotency_key="idk_abc"
        )
    assert captured["idempotency"] == "idk_abc"


@pytest.mark.parametrize(
    "bad_topic_id",
    ["abc", "topic_abc", "evt_abc", "", "../etc/passwd"],
)
async def test_publish_rejects_bad_topic_prefix(make_api_client, bad_topic_id: str) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="topic_id"):
            await api.publish_event(topic_id=bad_topic_id, payload={"x": 1})


async def test_publish_rejects_bad_payload_object_id_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="payload_object_id"):
            await api.publish_event(
                topic_id=TOPIC, payload_object_id="payload_abc"
            )


@pytest.mark.parametrize(
    "status,code,expected_cls",
    [
        (422, "INVALID", InvalidRequestError),
        (401, "UNAUTHORIZED", UnauthorizedError),
        (403, "FORBIDDEN", ForbiddenError),
        (403, "SUSPENDED", SuspendedError),
        (404, "NOT_FOUND", NotFoundError),
        (429, "RATE_LIMITED", RateLimitedError),
        (413, "PAYLOAD_TOO_LARGE", PayloadTooLargeError),
    ],
)
async def test_publish_error_envelope_routes(
    make_api_client, status: int, code: str, expected_cls: type
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=stub_pipe_error(error=code, message=f"{code} test"))

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        with pytest.raises(expected_cls):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})


async def test_publish_409_idempotency_conflict_specialization(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json=stub_pipe_error(
                error="CONFLICT",
                message="fingerprint mismatch",
                details={"reason": "idempotency_fingerprint_mismatch"},
            ),
        )

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        with pytest.raises(IdempotencyConflictError):
            await api.publish_event(
                topic_id=TOPIC, payload={"x": 1}, idempotency_key="idk_replay"
            )


# ---------- get_event -------------------------------------------------------


async def test_get_event_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_event_view(event_id=EVT))

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/events/{EVT}"): handler}
    ) as api:
        env = await api.get_event(topic_id=TOPIC, event_id=EVT)
        assert isinstance(env, MessageEnvelope)
        assert env.event_id == EVT


async def test_get_event_rejects_bad_evt_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="event_id"):
            await api.get_event(topic_id=TOPIC, event_id="forged_id")


async def test_get_event_ttl_expired_raises_specialization(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json=stub_pipe_error(
                error="NOT_FOUND",
                message="ttl expired",
                details={"reason": "ttl_expired", "ttl_expired_at": "x"},
                next_action="cursor_advance",
            ),
        )

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/events/{EVT}"): handler}
    ) as api:
        with pytest.raises(TTLExpiredError):
            await api.get_event(topic_id=TOPIC, event_id=EVT)


# ---------- list_events -----------------------------------------------------


async def test_list_events_normal_with_pagination(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        assert params["limit"] == "100"
        assert params["order"] == "asc"
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[stub_event_view(event_id=EVT, sequence_number=1)],
                after=EVT,
                after_seq=1,
                has_more=True,
            ),
        )

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/events"): handler}
    ) as api:
        page = await api.list_events(topic_id=TOPIC)
        assert isinstance(page, ListEventsPage)
        assert len(page.events) == 1
        assert page.next.has_more is True


async def test_list_events_with_all_query_params(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(req.url.params)
        return httpx.Response(200, json=stub_list_events_response())

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/events"): handler}
    ) as api:
        await api.list_events(
            topic_id=TOPIC,
            after=EVT,
            limit=50,
            order="desc",
            event_type="alpha.beta",
            expand="schema,payload_url",
            since="2026-01-01T00:00:00+00:00",
            until="2026-12-31T23:59:59+00:00",
        )
    assert captured["after"] == EVT
    assert captured["order"] == "desc"
    assert captured["type"] == "alpha.beta"
    assert captured["expand"] == "schema,payload_url"
    assert captured["since"].startswith("2026-01-01")
    assert captured["until"].startswith("2026-12-31")


async def test_list_events_clamped_header_surfaces_in_page(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_list_events_response(),
            headers={"X-AgenTrux-Pagination": "clamped"},
        )

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/events"): handler}
    ) as api:
        page = await api.list_events(topic_id=TOPIC, limit=10000)
        assert page.clamped is True


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
async def test_list_events_rejects_invalid_limit(make_api_client, bad_limit: int) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="limit must be > 0"):
            await api.list_events(topic_id=TOPIC, limit=bad_limit)


async def test_list_events_rejects_invalid_order(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="order"):
            await api.list_events(topic_id=TOPIC, order="random")


async def test_list_events_rejects_bad_after_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="after"):
            await api.list_events(topic_id=TOPIC, after="cursor_xxx")


# ---------- HTTP retry / refresh / backoff ----------------------------------


async def test_5xx_retries_then_succeeds(make_api_client) -> None:
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(500, json=stub_pipe_error(error="INTERNAL", message="boom"))
        return httpx.Response(200, json=stub_publish_response())

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        # Reduce backoff so test is fast
        api.INITIAL_BACKOFF_SECONDS = 0.01
        r = await api.publish_event(topic_id=TOPIC, payload={"x": 1})
    assert call_count[0] == 2
    assert isinstance(r, PublishResult)


async def test_5xx_exhausts_retries_raises_internal(make_api_client) -> None:
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        return httpx.Response(500, json=stub_pipe_error(error="INTERNAL", message="always"))

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        api.INITIAL_BACKOFF_SECONDS = 0.001
        with pytest.raises(InternalServerError):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})
    assert call_count[0] == api.MAX_RETRIES_SERVER_ERROR + 1


async def test_401_triggers_refresh_then_retries(base_url, make_jwt) -> None:
    call_count = [0]
    refreshed = [0]

    # Custom refresher: returns a fresh JWT each time.
    class StubRefresher:
        async def refresh(self, current_refresh_token: str):
            from agentrux.sdk.client import TokenBundle
            refreshed[0] += 1
            return TokenBundle(
                access_token=make_jwt(int(time.time()) + 3600),
                refresh_token=current_refresh_token,
                expires_at_unix=int(time.time()) + 3600,
            )

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                401,
                json=stub_pipe_error(error="UNAUTHORIZED", message="expired"),
            )
        return httpx.Response(200, json=stub_publish_response())

    transport = httpx.MockTransport(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("POST", f"/topics/{TOPIC}/events")
        else httpx.Response(404)
    )
    from agentrux.sdk.client import AgenTruxAPIClient
    http = httpx.AsyncClient(transport=transport, base_url=base_url)
    tm = TokenManager(
        access_token=make_jwt(int(time.time()) + 3600),
        refresh_token="art_x",
        refresher=StubRefresher(),
    )
    api = AgenTruxAPIClient(base_url=base_url, token_manager=tm, http=http)
    try:
        r = await api.publish_event(topic_id=TOPIC, payload={"x": 1})
    finally:
        await api.close()
    assert refreshed[0] == 1
    assert call_count[0] == 2
    assert isinstance(r, PublishResult)


async def test_client_credentials_refresh_fires_without_refresh_token(base_url, make_jwt) -> None:
    """The client_credentials path issues NO refresh_token but still needs
    automatic re-issue when the access_token nears expiry. The refresher
    must be invoked even though refresh_token is None (Codex 2nd review #1)."""
    from agentrux.sdk.client import AgenTruxAPIClient, TokenBundle, TokenManager

    refresher_calls = [0]

    class CCRefresher:
        async def refresh(self, current_refresh_token: str):
            refresher_calls[0] += 1
            return TokenBundle(
                access_token=make_jwt(int(time.time()) + 3600),
                refresh_token=None,
                expires_at_unix=int(time.time()) + 3600,
            )

    # Token already expiring → ensure_valid() must call the refresher
    # even though refresh_token is None.
    tm = TokenManager(
        access_token=make_jwt(int(time.time()) + 30),  # < 60s threshold
        refresh_token=None,
        refresher=CCRefresher(),
    )
    await tm.ensure_valid()
    assert refresher_calls[0] == 1
    # The new token is set (would still be < 60s otherwise — we set 3600s).
    assert tm.expires_at_unix > int(time.time()) + 3000


async def test_401_refresh_then_still_401_surfaces_not_loops(base_url, make_jwt) -> None:
    """If the refreshed token also returns 401, the SDK must surface the
    error rather than loop forever between refresh and request. The
    `already_refreshed_on_401` flag in _request_pipe is the guard."""

    refresh_count = [0]
    request_count = [0]

    class StubRefresher:
        async def refresh(self, current_refresh_token: str):
            from agentrux.sdk.client import TokenBundle
            refresh_count[0] += 1
            return TokenBundle(
                access_token=make_jwt(int(time.time()) + 3600),
                refresh_token=current_refresh_token,
                expires_at_unix=int(time.time()) + 3600,
            )

    def handler(req: httpx.Request) -> httpx.Response:
        request_count[0] += 1
        # ALWAYS 401: server says token is bad even after refresh.
        return httpx.Response(401, json=stub_pipe_error(error="UNAUTHORIZED", message="still bad"))

    transport = httpx.MockTransport(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("POST", f"/topics/{TOPIC}/events")
        else httpx.Response(404)
    )
    from agentrux.sdk.client import AgenTruxAPIClient
    http = httpx.AsyncClient(transport=transport, base_url=base_url)
    tm = TokenManager(
        access_token=make_jwt(int(time.time()) + 3600),
        refresh_token="art_x",
        refresher=StubRefresher(),
    )
    api = AgenTruxAPIClient(base_url=base_url, token_manager=tm, http=http)
    try:
        with pytest.raises(UnauthorizedError):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})
    finally:
        await api.close()
    # Critical: refresh fires exactly once (not in a loop), and the
    # request retried exactly once (not 5+ times).
    assert refresh_count[0] == 1, (
        f"refresh should fire once on 401, not loop: got {refresh_count[0]}"
    )
    assert request_count[0] == 2, (
        f"request should retry exactly once after refresh: got {request_count[0]}"
    )


async def test_401_without_refresher_surfaces_unauthorized(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=stub_pipe_error(error="UNAUTHORIZED", message="x"))

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        # TokenManager has no refresher → 401 propagates immediately.
        with pytest.raises(UnauthorizedError):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})


async def test_pipe_request_without_token_manager_raises(base_url) -> None:
    from agentrux.sdk.client import AgenTruxAPIClient

    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    http = httpx.AsyncClient(transport=transport, base_url=base_url)
    api = AgenTruxAPIClient(base_url=base_url, token_manager=None, http=http)
    try:
        with pytest.raises(SDKError, match="Bearer token"):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})
    finally:
        await api.close()


async def test_unparseable_error_body_wraps_to_internal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"not json")

    async with make_api_client({("POST", f"/topics/{TOPIC}/events"): handler}) as api:
        api.INITIAL_BACKOFF_SECONDS = 0.001
        with pytest.raises(InternalServerError):
            await api.publish_event(topic_id=TOPIC, payload={"x": 1})


# ---------- Payload upload / download / get --------------------------------


async def test_request_payload_upload_returns_ticket(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "payload_object_id": POB,
                "upload_url": "https://s3/sig",
                "upload_expires_at": "2026-05-24T11:00:00+00:00",
                "required_headers": {},
            },
        )

    async with make_api_client(
        {("POST", f"/topics/{TOPIC}/payloads"): handler}
    ) as api:
        t = await api.request_payload_upload(
            topic_id=TOPIC, content_type="application/octet-stream", size_bytes=100
        )
        assert t.payload_object_id == POB


async def test_get_payload_returns_download(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "payload_object_id": POB,
                "download_url": "https://s3/get",
                "download_expires_at": "x",
                "size_bytes": 4096,
            },
        )

    async with make_api_client(
        {("GET", f"/topics/{TOPIC}/payloads/{POB}"): handler}
    ) as api:
        d = await api.get_payload(topic_id=TOPIC, payload_object_id=POB)
        assert d.size_bytes == 4096


async def test_get_payload_rejects_bad_pob_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="payload_object_id"):
            await api.get_payload(topic_id=TOPIC, payload_object_id="payload_x")
