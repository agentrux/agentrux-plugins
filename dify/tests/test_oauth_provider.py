"""Unit tests for AgenTrux Dify plugin OAuth provider.

These tests stub out dify_plugin / werkzeug imports so they can run without
the Dify SDK installed (which is a binary release dependency on Linux).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub dify_plugin + werkzeug before importing the module under test.
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    if "dify_plugin" not in sys.modules:
        dp = types.ModuleType("dify_plugin")

        class ToolProvider:  # noqa: D401
            pass

        dp.ToolProvider = ToolProvider
        sys.modules["dify_plugin"] = dp

        entities = types.ModuleType("dify_plugin.entities")
        oauth = types.ModuleType("dify_plugin.entities.oauth")

        class ToolOAuthCredentials:
            def __init__(self, credentials, expires_at):
                self.credentials = credentials
                self.expires_at = expires_at

        oauth.ToolOAuthCredentials = ToolOAuthCredentials
        sys.modules["dify_plugin.entities"] = entities
        sys.modules["dify_plugin.entities.oauth"] = oauth

        errors = types.ModuleType("dify_plugin.errors")
        tool_errs = types.ModuleType("dify_plugin.errors.tool")

        class ToolProviderCredentialValidationError(Exception):
            pass

        class ToolProviderOAuthError(Exception):
            pass

        tool_errs.ToolProviderCredentialValidationError = (
            ToolProviderCredentialValidationError
        )
        tool_errs.ToolProviderOAuthError = ToolProviderOAuthError
        sys.modules["dify_plugin.errors"] = errors
        sys.modules["dify_plugin.errors.tool"] = tool_errs

    if "werkzeug" not in sys.modules:
        wz = types.ModuleType("werkzeug")

        class Request:  # minimal duck for type hints
            args: dict

        wz.Request = Request
        sys.modules["werkzeug"] = wz


_install_stubs()

# Make the build tree importable.
BUILD_ROOT = Path("/tmp/agentrux-dify-build")
sys.path.insert(0, str(BUILD_ROOT))

from provider import agentrux_api, agentrux_tools  # noqa: E402


# ---------------------------------------------------------------------------
# _is_url_allowed
# ---------------------------------------------------------------------------

def test_url_validation_accepts_https():
    assert agentrux_tools._is_url_allowed("https://api.agentrux.com")
    assert agentrux_api._is_url_allowed("https://api.agentrux.com")


def test_url_validation_accepts_loopback():
    assert agentrux_tools._is_url_allowed("http://localhost:8000")
    assert agentrux_tools._is_url_allowed("http://127.0.0.1:8000")


def test_url_validation_rejects_plain_http():
    assert not agentrux_tools._is_url_allowed("http://api.agentrux.com")
    assert not agentrux_tools._is_url_allowed("ftp://api.agentrux.com")


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def test_pkce_pair_unique_and_s256():
    import base64
    import hashlib

    v1, c1 = agentrux_tools._pkce_pair()
    v2, c2 = agentrux_tools._pkce_pair()
    assert v1 != v2
    # challenge = base64url(sha256(verifier))
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(v1.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert c1 == expected


# ---------------------------------------------------------------------------
# _validate_credentials  (client_credentials path)
# ---------------------------------------------------------------------------

def test_validate_skips_when_oauth_token_present():
    p = agentrux_tools.AgentruxToolsProvider()
    # access_token implies OAuth path already succeeded
    p._validate_credentials({"access_token": "ey..."})  # no exception


def test_validate_requires_base_url():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(agentrux_tools.ToolProviderCredentialValidationError):
        p._validate_credentials({"client_id": "script_x", "client_secret": "s"})


def test_validate_rejects_non_https_base_url():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(agentrux_tools.ToolProviderCredentialValidationError):
        p._validate_credentials(
            {
                "base_url": "http://api.agentrux.com",
                "client_id": "script_x",
                "client_secret": "s",
            }
        )


def test_validate_rejects_raw_uuid_client_id():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(
        agentrux_tools.ToolProviderCredentialValidationError, match="script_"
    ):
        p._validate_credentials(
            {
                "base_url": "https://api.agentrux.com",
                "client_id": "550e8400-e29b-41d4-a716-446655440000",
                "client_secret": "s",
            }
        )


def test_validate_calls_token_endpoint():
    p = agentrux_tools.AgentruxToolsProvider()
    fake = MagicMock()
    fake.status_code = 200
    with patch("provider.agentrux_tools.httpx.post", return_value=fake) as m:
        p._validate_credentials(
            {
                "base_url": "https://api.agentrux.com",
                "client_id": "script_abc",
                "client_secret": "secret123",
            }
        )
    args, kwargs = m.call_args
    assert args[0] == "https://api.agentrux.com/oauth/token"
    assert kwargs["data"]["grant_type"] == "client_credentials"
    assert kwargs["data"]["client_id"] == "script_abc"


def test_validate_raises_on_bad_credentials():
    p = agentrux_tools.AgentruxToolsProvider()
    fake = MagicMock()
    fake.status_code = 401
    with patch("provider.agentrux_tools.httpx.post", return_value=fake):
        with pytest.raises(
            agentrux_tools.ToolProviderCredentialValidationError, match="401"
        ):
            p._validate_credentials(
                {
                    "base_url": "https://api.agentrux.com",
                    "client_id": "script_abc",
                    "client_secret": "wrong",
                }
            )


# ---------------------------------------------------------------------------
# _oauth_get_authorization_url
# ---------------------------------------------------------------------------

def test_oauth_authorize_url_uses_pkce_s256():
    p = agentrux_tools.AgentruxToolsProvider()
    url = p._oauth_get_authorization_url(
        redirect_uri="https://dify.example.com/cb",
        system_credentials={
            "base_url": "https://api.agentrux.com",
            "client_id": "oauth-client_xyz",
        },
    )
    assert url.startswith("https://api.agentrux.com/oauth/authorize?")
    assert "response_type=code" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=oauth-client_xyz" in url
    assert "redirect_uri=https" in url


def test_oauth_authorize_url_rejects_http():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(agentrux_tools.ToolProviderOAuthError):
        p._oauth_get_authorization_url(
            redirect_uri="https://dify/cb",
            system_credentials={
                "base_url": "http://api.agentrux.com",
                "client_id": "x",
            },
        )


# ---------------------------------------------------------------------------
# _oauth_get_credentials  (authorization_code exchange)
# ---------------------------------------------------------------------------

class _FakeArgs(dict):
    """Dict subclass so .get works AND attribute assignment is allowed."""


def _make_request(code="abc", state="s1", error=None):
    args = _FakeArgs(code=code, state=state)
    if error:
        args["error"] = error
    req = types.SimpleNamespace(args=args)
    return req


def test_oauth_get_credentials_exchanges_code():
    p = agentrux_tools.AgentruxToolsProvider()
    # Prime PKCE state
    sys_creds = {
        "base_url": "https://api.agentrux.com",
        "client_id": "oauth-client_xyz",
    }
    p._oauth_get_authorization_url("https://dify/cb", sys_creds)
    state = next(iter(agentrux_tools._pkce_state.keys()))

    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "access_token": "ey.access",
        "refresh_token": "ey.refresh",
        "expires_in": 3600,
        "scope": "topic.read topic.write",
    }
    with patch("provider.agentrux_tools.httpx.post", return_value=fake) as m:
        creds = p._oauth_get_credentials(
            redirect_uri="https://dify/cb",
            system_credentials=sys_creds,
            request=_make_request(code="thecode", state=state),
        )

    args, kwargs = m.call_args
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "thecode"
    assert "code_verifier" in kwargs["data"]
    assert creds.credentials["access_token"] == "ey.access"
    assert creds.credentials["refresh_token"] == "ey.refresh"
    assert creds.credentials["base_url"] == "https://api.agentrux.com"
    assert creds.expires_at > 0


def test_oauth_get_credentials_rejects_bad_state():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(agentrux_tools.ToolProviderOAuthError, match="state"):
        p._oauth_get_credentials(
            redirect_uri="https://dify/cb",
            system_credentials={
                "base_url": "https://api.agentrux.com",
                "client_id": "x",
            },
            request=_make_request(code="c", state="never-issued"),
        )


def test_oauth_get_credentials_propagates_error_param():
    p = agentrux_tools.AgentruxToolsProvider()
    with pytest.raises(agentrux_tools.ToolProviderOAuthError, match="access_denied"):
        p._oauth_get_credentials(
            redirect_uri="https://dify/cb",
            system_credentials={
                "base_url": "https://api.agentrux.com",
                "client_id": "x",
            },
            request=_make_request(error="access_denied"),
        )


# ---------------------------------------------------------------------------
# _oauth_refresh_credentials
# ---------------------------------------------------------------------------

def test_oauth_refresh_uses_refresh_token():
    p = agentrux_tools.AgentruxToolsProvider()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "access_token": "ey.new",
        "refresh_token": "ey.newrefresh",
        "expires_in": 1800,
    }
    with patch("provider.agentrux_tools.httpx.post", return_value=fake) as m:
        creds = p._oauth_refresh_credentials(
            redirect_uri="https://dify/cb",
            system_credentials={
                "base_url": "https://api.agentrux.com",
                "client_id": "oauth-client_xyz",
            },
            credentials={"refresh_token": "ey.oldrefresh"},
        )

    kwargs = m.call_args.kwargs
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "ey.oldrefresh"
    assert creds.credentials["access_token"] == "ey.new"
    assert creds.credentials["refresh_token"] == "ey.newrefresh"


def test_oauth_refresh_falls_back_to_old_refresh_token():
    """If server doesn't rotate refresh_token, keep the old one."""
    p = agentrux_tools.AgentruxToolsProvider()
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"access_token": "ey.new", "expires_in": 1800}
    with patch("provider.agentrux_tools.httpx.post", return_value=fake):
        creds = p._oauth_refresh_credentials(
            redirect_uri="https://dify/cb",
            system_credentials={
                "base_url": "https://api.agentrux.com",
                "client_id": "x",
            },
            credentials={"refresh_token": "old"},
        )
    assert creds.credentials["refresh_token"] == "old"
