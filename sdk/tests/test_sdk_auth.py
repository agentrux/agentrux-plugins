"""SDK Phase 5.3 — Authenticator + HTTPClient (経路 B, retry/backoff, 401 fallback)."""

from __future__ import annotations

import httpx
import pytest

from agentrux.sdk import AgentRuxClient, SDKConfig
from agentrux.sdk.auth import Authenticator
from agentrux.sdk.errors import (
    AuthenticationError,
    CredentialRotatedError,
    RateLimitError,
    ServerError,
    TemporaryError,
)
from agentrux.sdk.http_client import HTTPClient

pytestmark = pytest.mark.unit


def _make_config(**kw) -> SDKConfig:
    base = dict(
        endpoint="https://api.example.com",
        client_id="crd_test",
        client_secret="aks_test",
    )
    base.update(kw)
    return SDKConfig(**base)


def _mock_transport(handler: callable) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_http_with_handler(handler: callable, **cfg_kw) -> tuple[HTTPClient, SDKConfig]:
    """HTTPClient with mock transport for test."""
    cfg = _make_config(**cfg_kw)
    http = HTTPClient(cfg)
    # 内部 client を mock transport で差し替え
    http._client = httpx.AsyncClient(
        base_url=cfg.endpoint,
        transport=_mock_transport(handler),
        headers={"User-Agent": cfg.user_agent},
    )
    return http, cfg


# ============================================================================
# Authenticator._issue
# ============================================================================


@pytest.mark.asyncio
async def test_issue_token_success_caches_with_lead() -> None:
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        assert req.url.path == "/oauth/token"
        return httpx.Response(
            200,
            json={
                "access_token": "aat_abc",
                "token_type": "Bearer",
                "expires_in": 600,
                "scope": "topic:top_x:write",
            },
        )

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        t1 = await auth.get_access_token()
        t2 = await auth.get_access_token()
        assert t1 == t2 == "aat_abc"
        # cache 利用で issue は 1 回のみ
        assert call_count["n"] == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_lead_seconds_triggers_proactive_refresh() -> None:
    """expires_in - now <= lead seconds の境界で先行再 issue."""
    call_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={"access_token": f"aat_{call_count['n']}", "token_type": "Bearer", "expires_in": 60},
        )

    http, cfg = _make_http_with_handler(handler, refresh_lead_seconds=120)
    auth = Authenticator(cfg, http)
    try:
        t1 = await auth.get_access_token()
        # expires_in=60 < lead 120 → 次の呼び出しで先行再 issue
        t2 = await auth.get_access_token()
        assert t1 != t2  # 別 token
        assert call_count["n"] == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_invalid_client_raises_credential_rotated() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        with pytest.raises(CredentialRotatedError, match="rotated"):
            await auth.get_access_token()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_other_401_raises_authentication_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        with pytest.raises(AuthenticationError, match="invalid_grant"):
            await auth.get_access_token()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_concurrent_get_access_token_single_issue() -> None:
    """並列で複数 get_access_token() しても _issue は 1 回しか走らない (lock)."""
    import asyncio

    call_count = {"n": 0}

    async def slow_handler(req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        await asyncio.sleep(0.05)
        return httpx.Response(
            200, json={"access_token": f"aat_{call_count['n']}", "token_type": "Bearer", "expires_in": 600}
        )

    # httpx.MockTransport は async handler を受け付けないので同期 wrapper
    def handler(req: httpx.Request) -> httpx.Response:
        # asyncio.run は test loop 内では NG。 同期で代用
        call_count["n"] += 1
        return httpx.Response(
            200, json={"access_token": f"aat_{call_count['n']}", "token_type": "Bearer", "expires_in": 600}
        )

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        results = await asyncio.gather(*[auth.get_access_token() for _ in range(5)])
        assert len(set(results)) == 1  # 全部同じ token
        assert call_count["n"] == 1  # _issue は 1 回のみ
    finally:
        await http.aclose()


# ============================================================================
# HTTPClient.request_with_auth (401 invalid_token fallback)
# ============================================================================


@pytest.mark.asyncio
async def test_request_with_auth_401_invalid_token_retries_once() -> None:
    """401 invalid_token を受けたら force_refresh して 1 回 retry."""
    state = {"token_call": 0, "api_call": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            state["token_call"] += 1
            return httpx.Response(
                200, json={"access_token": f"aat_v{state['token_call']}", "token_type": "Bearer", "expires_in": 600}
            )
        # /api/something
        state["api_call"] += 1
        if state["api_call"] == 1:
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"ok": True, "v": req.headers["authorization"]})

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        r = await http.request_with_auth("GET", "/api/something", auth=auth)
        assert r.status_code == 200
        assert state["api_call"] == 2  # 1 回目 401 + retry 1 回
        assert state["token_call"] == 2  # 初回 + force_refresh
        assert r.json()["v"] == "Bearer aat_v2"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_request_with_auth_401_invalid_client_does_not_retry() -> None:
    """401 invalid_client は即 raise (force_refresh しない)."""
    state = {"token_call": 0, "api_call": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            state["token_call"] += 1
            return httpx.Response(
                200, json={"access_token": "aat_x", "token_type": "Bearer", "expires_in": 600}
            )
        state["api_call"] += 1
        return httpx.Response(401, json={"error": "invalid_client"})

    http, cfg = _make_http_with_handler(handler)
    auth = Authenticator(cfg, http)
    try:
        with pytest.raises(CredentialRotatedError):
            await http.request_with_auth("GET", "/api/x", auth=auth)
        assert state["api_call"] == 1  # retry なし
        assert state["token_call"] == 1  # 初回のみ、 force_refresh しない
    finally:
        await http.aclose()


# ============================================================================
# HTTPClient.request_with_retry (TemporaryError / RateLimitError)
# ============================================================================


@pytest.mark.asyncio
async def test_retry_on_503_succeeds() -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] < 3:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json={"ok": True})

    http, _cfg = _make_http_with_handler(
        handler, max_retries=3, retry_base_seconds=0.001  # 高速 test
    )
    try:
        r = await http.request_with_retry("GET", "/api/x")
        assert r.status_code == 200
        assert state["n"] == 3  # 2 retry + 成功
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_retry_exhausts_then_raises_server_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    http, _cfg = _make_http_with_handler(handler, max_retries=2, retry_base_seconds=0.001)
    try:
        with pytest.raises(ServerError, match="upstream 503"):
            await http.request_with_retry("GET", "/api/x")
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_retry_429_uses_retry_after_header() -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0.001"})
        return httpx.Response(200)

    http, _cfg = _make_http_with_handler(handler, max_retries=1, retry_base_seconds=10.0)
    try:
        r = await http.request_with_retry("GET", "/api/x")
        assert r.status_code == 200
        assert state["n"] == 2
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_retry_429_exhausts_raises_rate_limit() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "0"})

    http, _cfg = _make_http_with_handler(handler, max_retries=1, retry_base_seconds=0.001)
    try:
        with pytest.raises(RateLimitError) as ei:
            await http.request_with_retry("GET", "/api/x")
        assert ei.value.retry_after == 0.0
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_no_retry_for_4xx_other_than_429() -> None:
    state = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        state["n"] += 1
        return httpx.Response(404, json={"error": "not_found"})

    http, _cfg = _make_http_with_handler(handler, max_retries=3, retry_base_seconds=0.001)
    try:
        r = await http.request_with_retry("GET", "/api/x")
        assert r.status_code == 404
        assert state["n"] == 1  # retry なし
    finally:
        await http.aclose()


# ============================================================================
# AgentRuxClient end-to-end (constructor → request → aclose)
# ============================================================================


@pytest.mark.asyncio
async def test_agentrux_client_end_to_end_authenticated_request() -> None:
    state = {"token_call": 0, "api_call": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            state["token_call"] += 1
            return httpx.Response(
                200, json={"access_token": "aat_e2e", "token_type": "Bearer", "expires_in": 600}
            )
        state["api_call"] += 1
        assert req.headers["authorization"] == "Bearer aat_e2e"
        return httpx.Response(200, json={"ok": True})

    client = AgentRuxClient(
        endpoint="https://api.example.com",
        client_id="crd_e2e",
        client_secret="aks_e2e",
    )
    # mock transport を facade の http に差し替え
    client._http._client = httpx.AsyncClient(
        base_url=client.config.endpoint,
        transport=_mock_transport(handler),
        headers={"User-Agent": client.config.user_agent},
    )
    try:
        r = await client._request("GET", "/api/echo")
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        assert state["token_call"] == 1
        assert state["api_call"] == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_temporary_error_after_network_failures() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated")

    http, _cfg = _make_http_with_handler(handler, max_retries=2, retry_base_seconds=0.001)
    try:
        with pytest.raises(TemporaryError, match="network error"):
            await http.request_with_retry("GET", "/api/x")
    finally:
        await http.aclose()
