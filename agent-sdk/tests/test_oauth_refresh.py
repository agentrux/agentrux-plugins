"""Smoke tests for the OAuth 2.1 refresh + persistence wiring (v0.3 SDK).

These pin the post-Codex-review contract against the v0.3 SDK surface:

- ``OAuthRefreshTokenRefresher`` posts form-encoded ``/oauth/token`` with
  ``grant_type=refresh_token`` + ``client_id``; rejects construction
  without ``oauth_client_id``.
- ``AgenTruxClient.from_access_token`` wires up auto-refresh only when
  both ``refresh_token`` and ``oauth_client_id`` are present.
- The agent-sdk plugin's per-profile lock + atomic credentials write
  survives concurrent writers without losing a section.
"""
from __future__ import annotations

import configparser
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from agentrux.sdk.client import (
    OAuthRefreshTokenRefresher,
    TokenBundle,
)
from agentrux.sdk.facade import AgenTruxClient


# ---------------------------------------------------------------------------
# OAuthRefreshTokenRefresher contract
# ---------------------------------------------------------------------------


def test_refresher_requires_oauth_client_id() -> None:
    with pytest.raises(ValueError, match="oauth_client_id"):
        OAuthRefreshTokenRefresher(
            base_url="https://api.example.com",
            oauth_client_id="",
        )


@pytest.mark.asyncio
async def test_refresher_posts_form_encoded_to_oauth_token() -> None:
    captured: dict = {}

    class _StubResp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "access_token": "AT-new",
                "refresh_token": "RT-new",
                "token_type": "Bearer",
                "expires_in": 3600,
            }

    class _StubAsyncClient:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self) -> "_StubAsyncClient":
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def post(self, url: str, *, data, headers) -> _StubResp:
            captured["url"] = url
            captured["data"] = data
            captured["headers"] = headers
            return _StubResp()

    with patch.object(httpx, "AsyncClient", _StubAsyncClient):
        refresher = OAuthRefreshTokenRefresher(
            base_url="https://api.example.com",
            oauth_client_id="oauth-client_abc",
        )
        bundle = await refresher.refresh("RT-old")

    assert captured["url"] == "https://api.example.com/oauth/token"
    assert captured["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "RT-old",
        "client_id": "oauth-client_abc",
    }
    assert captured["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert isinstance(bundle, TokenBundle)
    assert bundle.access_token == "AT-new"
    assert bundle.refresh_token == "RT-new"
    assert bundle.expires_at_unix > int(time.time())


# ---------------------------------------------------------------------------
# AgenTruxClient.from_access_token autowiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_access_token_skips_refresher_without_oauth_client_id() -> None:
    client = await AgenTruxClient.from_access_token(
        base_url="https://api.example.com",
        access_token="AT-explicit",
        refresh_token="RT-orphaned",
    )
    # No oauth_client_id → no refresher wired, refresh stays disabled.
    assert client._token_manager._refresher is None  # noqa: SLF001
    await client.close()


@pytest.mark.asyncio
async def test_from_access_token_builds_refresher_when_both_present() -> None:
    client = await AgenTruxClient.from_access_token(
        base_url="https://api.example.com",
        access_token="AT-explicit",
        refresh_token="RT-good",
        oauth_client_id="oauth-client_zzz",
    )
    assert isinstance(
        client._token_manager._refresher,  # noqa: SLF001
        OAuthRefreshTokenRefresher,
    )
    await client.close()


# ---------------------------------------------------------------------------
# Per-profile lock + atomic write contract (agent-sdk plugin)
# ---------------------------------------------------------------------------


def test_atomic_write_section_under_concurrent_writers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Two threads writing different profiles must not lose either section.

    Reproduces the concern raised in the Codex review: write-back must
    serialise within a profile but also coexist with sibling profile
    writes via the per-profile lock model. We hold the lock around each
    write, then assert both sections survive.
    """
    monkeypatch.setenv("HOME", str(tmp_path))

    # Re-import after HOME flip so the module-level Path constants pick
    # up the new home directory.
    import importlib

    import agentrux_agent_tools.cli as cli_mod
    import agentrux_agent_tools.toolkit as toolkit_mod

    importlib.reload(cli_mod)
    importlib.reload(toolkit_mod)

    def writer(profile: str, token: str) -> None:
        for _ in range(50):
            with cli_mod._profile_lock(profile):
                toolkit_mod._atomic_write_section(profile, {
                    "access_token": token,
                    "refresh_token": f"RT-{token}",
                    "expires_at": str(int(time.time()) + 3600),
                })

    t1 = threading.Thread(target=writer, args=("alpha", "AT-A"))
    t2 = threading.Thread(target=writer, args=("beta", "AT-B"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / ".agentrux" / "credentials")
    assert cfg["alpha"]["access_token"] == "AT-A"
    assert cfg["beta"]["access_token"] == "AT-B"
    # Mode 0600 must be preserved across the rename.
    mode = (tmp_path / ".agentrux" / "credentials").stat().st_mode & 0o777
    assert mode == 0o600


def test_persist_hook_writes_token_bundle_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """The hook returned by _make_persist_hook must rewrite the section."""
    monkeypatch.setenv("HOME", str(tmp_path))

    import importlib

    import agentrux_agent_tools.toolkit as toolkit_mod

    importlib.reload(toolkit_mod)

    # Seed an existing section so the hook is doing a merge, not a create.
    toolkit_mod._atomic_write_section("default", {
        "access_token": "AT-old",
        "refresh_token": "RT-old",
        "expires_at": "1",
        "client_id": "oauth-client_xyz",
    })

    hook = toolkit_mod._make_persist_hook("default")
    bundle = TokenBundle(
        access_token="AT-rotated",
        refresh_token="RT-rotated",
        expires_at_unix=int(time.time()) + 1800,
    )
    hook(bundle)

    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / ".agentrux" / "credentials")
    sec = cfg["default"]
    assert sec["access_token"] == "AT-rotated"
    assert sec["refresh_token"] == "RT-rotated"
    assert sec["client_id"] == "oauth-client_xyz"  # untouched
    assert int(sec["expires_at"]) == bundle.expires_at_unix


# ---------------------------------------------------------------------------
# Error coercion for the device-flow poll loop
# ---------------------------------------------------------------------------

def test_coerce_rfc_oauth_error() -> None:
    from agentrux_agent_tools.cli import _coerce_oauth_error
    assert _coerce_oauth_error({"error": "authorization_pending"}) == "authorization_pending"
    assert _coerce_oauth_error({"error": "access_denied"}) == "access_denied"
    assert _coerce_oauth_error({}) == ""
    assert _coerce_oauth_error(None) == ""


def test_coerce_agentrux_wrapper_pending() -> None:
    """Server returns AgenTrux's internal error shape for pending poll.

    RFC 8628 §3.5 prescribes ``{"error": "authorization_pending"}`` but
    the live backend currently emits
    ``{"error": {"code": "RATE_LIMITED", "message": "user has not yet
    approved"}}`` for that same case. The CLI must not crash on the
    dict shape and must translate it back to the RFC vocabulary so the
    polling loop keeps waiting instead of bailing.
    """
    from agentrux_agent_tools.cli import _coerce_oauth_error
    payload = {"error": {"code": "RATE_LIMITED", "message": "user has not yet approved"}}
    assert _coerce_oauth_error(payload) == "authorization_pending"


def test_coerce_unknown_dict_error_passes_code_through() -> None:
    from agentrux_agent_tools.cli import _coerce_oauth_error
    payload = {"error": {"code": "FORBIDDEN", "message": "no"}}
    assert _coerce_oauth_error(payload) == "FORBIDDEN"
