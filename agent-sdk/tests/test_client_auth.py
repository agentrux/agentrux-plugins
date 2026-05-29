"""Auth/refresh coverage for the local AgentRuxClient + toolkit profile path.

These pin the *current* agent-sdk auth surface (agentrux_agent_tools.client +
toolkit Path 3), replacing the deleted test_oauth_refresh.py which tested a
removed legacy SDK (OAuthRefreshTokenRefresher / TokenBundle / from_access_token).

Live code under test:
  - AgentRuxClient._ensure_token  : refresh-first, client_credentials fallback
  - AgentRuxClient._issue_refresh : form-encoded refresh_token grant + 4xx handling
  - AgentRuxClient._consume_token_response : single-use refresh rotation
  - AgenTruxToolkit.create() Path 3 : ~/.agentrux profile → client w/ refresh wiring
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentrux_agent_tools.client import (
    AgentRuxClient,
    AuthExpiredError,
)


class _FakeResp:
    def __init__(
        self, status_code: int, body: dict[str, Any] | None = None, text: str = ""
    ) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._body


def _client(**kw: Any) -> AgentRuxClient:
    c = AgentRuxClient(base_url="https://api.example.com", **kw)
    # Replace the real httpx client so no socket is ever opened.
    c._http = MagicMock()  # noqa: SLF001
    c._http.aclose = AsyncMock()  # noqa: SLF001  (close() awaits aclose())
    return c


# ---------------------------------------------------------------------------
# _issue_refresh contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_refresh_posts_form_encoded_with_client_id() -> None:
    c = _client(refresh_token="RT-old", client_id_for_refresh="oauth-client_abc")
    post = AsyncMock(
        return_value=_FakeResp(
            200,
            {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 600},
        )
    )
    c._http.post = post  # noqa: SLF001

    token = await c._ensure_token()  # noqa: SLF001

    assert token == "AT-new"
    args, kwargs = post.call_args
    assert args[0] == "/oauth/token"
    assert kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "RT-old",
        "client_id": "oauth-client_abc",
    }
    assert kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    # Single-use rotation: the new refresh_token must replace the old one.
    assert c._refresh_token == "RT-new"  # noqa: SLF001
    await c.close()


@pytest.mark.asyncio
async def test_issue_refresh_4xx_clears_token_and_raises_authexpired() -> None:
    c = _client(refresh_token="RT-dead", client_id_for_refresh="oauth-client_abc")
    c._http.post = AsyncMock(
        return_value=_FakeResp(400, {}, text="invalid_grant")
    )  # noqa: SLF001

    with pytest.raises(AuthExpiredError, match="agentrux login"):
        await c._ensure_token()  # noqa: SLF001

    # Dead refresh_token must be dropped so we don't loop on it.
    assert c._refresh_token is None  # noqa: SLF001
    await c.close()


@pytest.mark.asyncio
async def test_ensure_token_falls_back_to_client_credentials_on_dead_refresh() -> None:
    """When the refresh_token is rejected but client_credentials exist,
    _ensure_token must fall through to the cc grant rather than raise."""
    c = _client(
        refresh_token="RT-dead",
        client_id="crd_x",
        client_secret="aks_y",
        client_id_for_refresh="oauth-client_abc",
    )
    responses = [
        _FakeResp(401, {}, text="invalid_grant"),  # refresh rejected
        _FakeResp(200, {"access_token": "AT-cc", "expires_in": 600}),  # cc grant
    ]
    c._http.post = AsyncMock(side_effect=responses)  # noqa: SLF001

    token = await c._ensure_token()  # noqa: SLF001

    assert token == "AT-cc"
    # Second call was the client_credentials grant.
    second_kwargs = c._http.post.call_args_list[1].kwargs  # noqa: SLF001
    assert second_kwargs["data"]["grant_type"] == "client_credentials"
    assert second_kwargs["data"]["client_id"] == "crd_x"
    await c.close()


@pytest.mark.asyncio
async def test_externally_managed_token_is_trusted_without_refresh() -> None:
    """A caller-supplied access_token (expires_at unknown) is used as-is —
    no /oauth/token round-trip."""
    c = _client(token="AT-explicit")
    c._http.post = AsyncMock(
        side_effect=AssertionError("must not call /oauth/token")
    )  # noqa: SLF001

    token = await c._ensure_token()  # noqa: SLF001

    assert token == "AT-explicit"
    await c.close()


# ---------------------------------------------------------------------------
# toolkit Path 3: device-flow profile load wires refresh fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toolkit_profile_path_wires_refresh_fields() -> None:
    import agentrux_agent_tools.toolkit as tk

    creds = {
        "base_url": "https://api.example.com",
        "access_token": "AT-profile",
        "refresh_token": "RT-profile",
        "client_id": "oauth-client_zzz",
        "expires_at": "0",
    }
    ctor = MagicMock(return_value=MagicMock(close=AsyncMock()))
    with (
        patch.object(tk, "_load_cli_profile", return_value=creds),
        patch.object(tk, "AgentRuxClient", ctor),
    ):
        toolkit = await tk.AgenTruxToolkit.create(profile="default")

    ctor.assert_called_once_with(
        base_url="https://api.example.com",
        token="AT-profile",
        refresh_token="RT-profile",
        client_id_for_refresh="oauth-client_zzz",
    )
    await toolkit.close()
