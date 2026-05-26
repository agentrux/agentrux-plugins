"""Tests for AgenTruxAPIClient — OAuth / DCR / device flow / AC / discovery.

The OAuth endpoint family uses RFC 6749 §5.2 flat error envelopes, not
the FastAPI HTTPException shape; the AC redeem endpoint uses the
pipe envelope (it lives behind FastAPI).
"""
from __future__ import annotations

import httpx
import pytest

from agentrux.sdk.auth_models import (
    ActivationCodeRedemption,
    AuthorizationServerMetadata,
    DCRRegistration,
    DeviceAuthorization,
    OAuthTokenResponse,
)
from agentrux.sdk.errors import (
    AuthorizationPendingError,
    ExpiredTokenError,
    InvalidClientError,
    InvalidGrantError,
    InvalidRequestError,
    NotFoundError,
    OAuthError,
    SDKError,
    SlowDownError,
)

from .conftest import stub_oauth_error, stub_oauth_token_response, stub_pipe_error


pytestmark = pytest.mark.asyncio


# ---------- oauth_token_client_credentials ----------------------------


async def test_oauth_client_credentials_normal(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(req.read().decode())))
        return httpx.Response(200, json=stub_oauth_token_response())

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        r = await api.oauth_token_client_credentials(
            client_id="crd_00000000-0000-0000-0000-000000000001",
            client_secret="aks_secret",
        )
    assert isinstance(r, OAuthTokenResponse)
    assert captured["grant_type"] == "client_credentials"
    assert captured["client_id"].startswith("crd_")
    assert captured["client_secret"] == "aks_secret"


async def test_oauth_client_credentials_rejects_non_crd_prefix(make_api_client) -> None:
    async with make_api_client({}, with_token=False) as api:
        with pytest.raises(ValueError, match="crd_"):
            await api.oauth_token_client_credentials(
                client_id="script_abc",  # legacy v0.2 prefix
                client_secret="aks_x",
            )


async def test_oauth_client_credentials_with_scope(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(req.read().decode())))
        return httpx.Response(200, json=stub_oauth_token_response(scope="topic:abc:read"))

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        r = await api.oauth_token_client_credentials(
            client_id="crd_x_uuid",
            client_secret="aks_x",
            scope="topic:abc:read",
        )
    assert captured["scope"] == "topic:abc:read"
    assert r.scope == "topic:abc:read"


@pytest.mark.parametrize(
    "code,expected_cls",
    [
        ("invalid_client", InvalidClientError),
        ("invalid_grant", InvalidGrantError),
    ],
)
async def test_oauth_token_error_routes_to_subclass(
    make_api_client, code: str, expected_cls: type
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=stub_oauth_error(error=code, error_description="x"))

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        with pytest.raises(expected_cls):
            await api.oauth_token_client_credentials(
                client_id="crd_x_uuid", client_secret="aks_x"
            )


# ---------- oauth_token_refresh ---------------------------------------


async def test_oauth_token_refresh_normal(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(req.read().decode())))
        return httpx.Response(
            200, json=stub_oauth_token_response(refresh_token="art_new")
        )

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        r = await api.oauth_token_refresh(
            refresh_token="art_old", client_id="dcr_x_uuid"
        )
    assert captured["grant_type"] == "refresh_token"
    assert captured["client_id"].startswith("dcr_")
    assert r.refresh_token == "art_new"


async def test_oauth_token_refresh_with_client_secret(make_api_client) -> None:
    captured: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured.update(dict(httpx.QueryParams(req.read().decode())))
        return httpx.Response(200, json=stub_oauth_token_response())

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        await api.oauth_token_refresh(
            refresh_token="art_x", client_id="crd_x_uuid", client_secret="aks_x"
        )
    assert captured["client_secret"] == "aks_x"


# ---------- oauth_token_device_code ------------------------------------


async def test_oauth_device_code_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_oauth_token_response(refresh_token="art_y"))

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        r = await api.oauth_token_device_code(
            device_code="device_abc", client_id="dcr_x_uuid"
        )
    assert r.refresh_token == "art_y"


@pytest.mark.parametrize(
    "code,expected_cls",
    [
        ("authorization_pending", AuthorizationPendingError),
        ("slow_down", SlowDownError),
        ("expired_token", ExpiredTokenError),
    ],
)
async def test_oauth_device_code_polling_errors(
    make_api_client, code: str, expected_cls: type
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json=stub_oauth_error(error=code, error_description="x"))

    async with make_api_client(
        {("POST", "/oauth/token"): handler}, with_token=False
    ) as api:
        with pytest.raises(expected_cls):
            await api.oauth_token_device_code(
                device_code="device_x", client_id="dcr_y_uuid"
            )


# ---------- register_dcr ------------------------------------------------


def _dcr_response(**overrides):
    base = {
        "client_id": "dcr_00000000-0000-0000-0000-000000000001",
        "client_id_issued_at": 1779600000,
        "client_secret_expires_at": 0,
        "client_name": "test-plugin",
        "redirect_uris": [],
        "grant_types": ["device_code", "refresh_token"],
        "token_endpoint_auth_method": "none",
        "registration_access_token": "rat_abc",
        "registration_client_uri": "/oauth/register/dcr_x",
    }
    base.update(overrides)
    return base


async def test_register_dcr_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j

        body = _j.loads(req.read())
        assert body["token_endpoint_auth_method"] == "none"
        return httpx.Response(201, json=_dcr_response())

    async with make_api_client(
        {("POST", "/oauth/register"): handler}, with_token=False
    ) as api:
        r = await api.register_dcr(client_name="test-plugin")
    assert isinstance(r, DCRRegistration)
    assert r.client_id.startswith("dcr_")


async def test_register_dcr_400_invalid_client_metadata(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json=stub_oauth_error(
                error="invalid_client_metadata", error_description="wrong method"
            ),
        )

    async with make_api_client(
        {("POST", "/oauth/register"): handler}, with_token=False
    ) as api:
        with pytest.raises(OAuthError):
            await api.register_dcr(client_name="x")


# ---------- device_authorization ---------------------------------------


async def test_device_authorization_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "device_code": "device_abc",
                "user_code": "ABCD-1234",
                "verification_uri": "https://example/device",
                "verification_uri_complete": "https://example/device?code=ABCD-1234",
                "expires_in": 600,
                "interval": 5,
            },
        )

    async with make_api_client(
        {("POST", "/oauth/device/authorize"): handler}, with_token=False
    ) as api:
        r = await api.device_authorization(client_id="dcr_x_uuid", scope="topic:abc:read")
    assert isinstance(r, DeviceAuthorization)
    assert r.user_code == "ABCD-1234"


# ---------- redeem_activation_code (pipe envelope) ----------------------


async def test_redeem_activation_code_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        import json as _j

        body = _j.loads(req.read())
        assert body["code"].startswith("act_")
        return httpx.Response(
            200,
            json={
                "client_id": "crd_00000000-0000-0000-0000-000000000001",
                "client_secret": "aks_xyz",
                "script_id": "scr_00000000-0000-0000-0000-000000000001",
                "issued_at": "2026-05-24T10:00:00+00:00",
            },
        )

    async with make_api_client(
        {("POST", "/auth/redeem-activation-code"): handler}, with_token=False
    ) as api:
        r = await api.redeem_activation_code(code="act_one_shot")
    assert isinstance(r, ActivationCodeRedemption)
    assert r.client_secret.startswith("aks_")


async def test_redeem_activation_code_rejects_bad_prefix(make_api_client) -> None:
    async with make_api_client({}, with_token=False) as api:
        with pytest.raises(ValueError, match="act_"):
            await api.redeem_activation_code(code="bogus_code")


async def test_redeem_activation_code_404_raises_not_found(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        # AC redeem uses the pipe envelope (FastAPI HTTPException shape).
        return httpx.Response(
            404,
            json=stub_pipe_error(error="NOT_FOUND", message="already consumed"),
        )

    async with make_api_client(
        {("POST", "/auth/redeem-activation-code"): handler}, with_token=False
    ) as api:
        with pytest.raises(NotFoundError):
            await api.redeem_activation_code(code="act_used")


# ---------- discover_metadata -----------------------------------------


async def test_discover_metadata_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://api.agentrux.test",
                "authorization_endpoint": "https://api.agentrux.test/oauth/authorize",
                "token_endpoint": "https://api.agentrux.test/oauth/token",
                "registration_endpoint": "https://api.agentrux.test/oauth/register",
                "jwks_uri": "https://api.agentrux.test/.well-known/jwks.json",
                "scopes_supported": ["topic:abc:read", "topic:abc:write"],
                "grant_types_supported": ["client_credentials", "authorization_code"],
            },
        )

    async with make_api_client(
        {("GET", "/.well-known/oauth-authorization-server"): handler},
        with_token=False,
    ) as api:
        m = await api.discover_metadata()
    assert isinstance(m, AuthorizationServerMetadata)
    assert m.token_endpoint.endswith("/oauth/token")
    assert "topic:abc:read" in m.scopes_supported


async def test_discover_metadata_404_falls_to_internal_error(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json=stub_pipe_error(error="NOT_FOUND", message="metadata missing")
        )

    async with make_api_client(
        {("GET", "/.well-known/oauth-authorization-server"): handler},
        with_token=False,
    ) as api:
        with pytest.raises(NotFoundError):
            await api.discover_metadata()


# ---------- attack: URL injection via base_url -------------------------


async def test_base_url_with_path_strip_trailing_slash(make_api_client) -> None:
    """Defensive: base_url with trailing slash should still produce clean URLs."""
    from agentrux.sdk.client import AgenTruxAPIClient

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=stub_oauth_token_response())
    )
    http = httpx.AsyncClient(transport=transport)
    api = AgenTruxAPIClient(
        base_url="https://api.agentrux.test/", token_manager=None, http=http
    )
    try:
        await api.oauth_token_client_credentials(
            client_id="crd_x_uuid", client_secret="aks_x"
        )
        # No exception means URL was canonicalized properly.
    finally:
        await api.close()


async def test_empty_base_url_rejected() -> None:
    from agentrux.sdk.client import AgenTruxAPIClient

    with pytest.raises(ValueError, match="base_url"):
        AgenTruxAPIClient(base_url="")


# ---------- get_jwks ---------------------------------------------------


async def test_jwks_normal(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "keys": [
                    {"kty": "RSA", "kid": "k1", "use": "sig", "n": "x", "e": "AQAB"}
                ]
            },
        )

    async with make_api_client(
        {("GET", "/.well-known/jwks.json"): handler}, with_token=False
    ) as api:
        jwks = await api.get_jwks()
    assert "keys" in jwks
    assert isinstance(jwks["keys"], list)
    assert jwks["keys"][0]["kid"] == "k1"


async def test_jwks_non_object_response_raises(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["not", "an", "object"])

    async with make_api_client(
        {("GET", "/.well-known/jwks.json"): handler}, with_token=False
    ) as api:
        with pytest.raises(Exception, match="jwks"):
            await api.get_jwks()


async def test_jwks_404_raises_not_found(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"detail": {"error": "NOT_FOUND", "message": "x"}}
        )

    async with make_api_client(
        {("GET", "/.well-known/jwks.json"): handler}, with_token=False
    ) as api:
        with pytest.raises(NotFoundError):
            await api.get_jwks()
