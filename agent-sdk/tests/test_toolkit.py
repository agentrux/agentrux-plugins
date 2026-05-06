"""Unit tests for AgenTruxToolkit and TOOL_DEFINITIONS."""
from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub the agentrux.sdk.facade module so the real SDK is not required.
# This must happen BEFORE importing toolkit.
# ---------------------------------------------------------------------------
_fake_facade = ModuleType("agentrux.sdk.facade")
_fake_client = ModuleType("agentrux.sdk.client")


class _StubClient:
    """Minimal stub for AgenTruxClient used during import."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    async def close(self) -> None:
        pass


from dataclasses import dataclass


@dataclass(frozen=True)
class _StubTokenBundle:
    access_token: str
    refresh_token: str
    expires_at: int


_fake_facade.AgenTruxClient = _StubClient  # type: ignore[attr-defined]
_fake_client.TokenBundle = _StubTokenBundle  # type: ignore[attr-defined]

# Build the parent package path so Python resolves the SDK submodules.
for mod_name in (
    "agentrux", "agentrux.sdk", "agentrux.sdk.facade", "agentrux.sdk.client",
):
    if mod_name not in sys.modules:
        if mod_name == "agentrux.sdk.facade":
            sys.modules[mod_name] = _fake_facade
        elif mod_name == "agentrux.sdk.client":
            sys.modules[mod_name] = _fake_client
        else:
            pkg = ModuleType(mod_name)
            pkg.__path__ = []  # type: ignore[attr-defined]
            sys.modules[mod_name] = pkg

from agentrux_agent_tools.toolkit import TOOL_DEFINITIONS, AgenTruxToolkit


# ---------------------------------------------------------------------------
# Tests for TOOL_DEFINITIONS
# ---------------------------------------------------------------------------

class TestToolDefinitions:
    """Tests for the static TOOL_DEFINITIONS list."""

    EXPECTED_TOOL_NAMES = {"publish_event", "list_events", "get_event", "wait_for_event"}

    def test_contains_expected_tool_names(self) -> None:
        """TOOL_DEFINITIONS contains all four expected tool names."""
        actual_names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
        assert actual_names == self.EXPECTED_TOOL_NAMES

    def test_each_definition_has_openai_structure(self) -> None:
        """Each definition has the OpenAI function-calling structure."""
        for defn in TOOL_DEFINITIONS:
            assert defn["type"] == "function"
            func = defn["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            params = func["parameters"]
            assert params["type"] == "object"
            assert "properties" in params
            assert "required" in params


# ---------------------------------------------------------------------------
# Tests for get_tools / get_tools_anthropic
# ---------------------------------------------------------------------------

class TestGetTools:
    """Tests for tool format conversion methods."""

    @pytest.fixture()
    def toolkit(self) -> AgenTruxToolkit:
        mock_client = MagicMock()
        return AgenTruxToolkit(client=mock_client)

    def test_get_tools_returns_openai_format(self, toolkit: AgenTruxToolkit) -> None:
        """get_tools() returns the OpenAI function-calling list."""
        tools = toolkit.get_tools()
        assert isinstance(tools, list)
        assert len(tools) == len(TOOL_DEFINITIONS)
        for t in tools:
            assert "type" in t
            assert "function" in t

    def test_get_tools_anthropic_returns_anthropic_format(
        self, toolkit: AgenTruxToolkit
    ) -> None:
        """get_tools_anthropic() returns Anthropic tool_use format."""
        tools = toolkit.get_tools_anthropic()
        assert isinstance(tools, list)
        assert len(tools) == len(TOOL_DEFINITIONS)
        for t in tools:
            assert "name" in t
            assert "description" in t
            assert "input_schema" in t
            # Must NOT have the OpenAI wrapper keys
            assert "type" not in t
            assert "function" not in t

    def test_anthropic_tool_names_match_openai(
        self, toolkit: AgenTruxToolkit
    ) -> None:
        """Anthropic and OpenAI formats expose the same tool names."""
        openai_names = {t["function"]["name"] for t in toolkit.get_tools()}
        anthropic_names = {t["name"] for t in toolkit.get_tools_anthropic()}
        assert openai_names == anthropic_names


# ---------------------------------------------------------------------------
# Tests for AgenTruxToolkit.create() validation
# ---------------------------------------------------------------------------

class TestToolkitCreate:
    """Tests for AgenTruxToolkit.create() parameter validation."""

    @pytest.mark.asyncio
    async def test_create_raises_when_no_credentials_anywhere(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """create() raises ValueError when none of the 3 auth paths can succeed.

        With every env var unset, no explicit args, and an empty
        credentials file, the toolkit must surface the device-flow hint
        rather than silently succeed.
        """
        monkeypatch.delenv("AGENTRUX_BASE_URL", raising=False)
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("AGENTRUX_ACCESS_TOKEN", raising=False)
        monkeypatch.delenv("AGENTRUX_REFRESH_TOKEN", raising=False)
        monkeypatch.delenv("AGENTRUX_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)
        # The toolkit's _CREDENTIALS_PATH is computed at import time, so
        # an env-var flip alone won't redirect it. Override the module
        # constant directly to point at an empty tmp_path.
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk
        monkeypatch.setattr(
            _tk, "_CREDENTIALS_PATH", _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_access_token_without_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Path 1 still validates base_url when an access_token is supplied."""
        monkeypatch.delenv("AGENTRUX_BASE_URL", raising=False)
        with pytest.raises(ValueError, match="base_url is required"):
            await AgenTruxToolkit.create(access_token="AT-explicit")

    @pytest.mark.asyncio
    async def test_create_raises_when_only_script_id_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Path 2 (client_credentials) requires both script_id AND client_secret.

        Missing one half of the pair must NOT silently succeed; the
        toolkit falls through to Path 3 (profile load) and surfaces the
        device-flow hint when no credentials file exists.
        """
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk
        monkeypatch.setattr(
            _tk, "_CREDENTIALS_PATH", _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_only_client_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Path 2 (client_credentials) requires both script_id AND client_secret."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk
        monkeypatch.setattr(
            _tk, "_CREDENTIALS_PATH", _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()


# ---------------------------------------------------------------------------
# Tests for env var naming convention
# ---------------------------------------------------------------------------

class TestEnvVarNaming:
    """Verify the expected environment variable names are used."""

    @pytest.mark.asyncio
    async def test_env_vars_are_agentrux_prefixed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() reads AGENTRUX_BASE_URL, AGENTRUX_SCRIPT_ID,
        AGENTRUX_CLIENT_SECRET, and AGENTRUX_INVITE_CODE."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        monkeypatch.setenv("AGENTRUX_INVITE_CODE", "invite-xyz")

        # Patch AgenTruxClient to avoid real HTTP calls
        mock_client_cls = MagicMock()
        mock_instance = AsyncMock()
        mock_instance.get_token = AsyncMock(
            return_value={"access_token": "tok", "refresh_token": "rtok"}
        )
        mock_instance.redeem_grant = AsyncMock()
        mock_instance.close = AsyncMock()
        mock_client_cls.return_value = mock_instance

        with patch("agentrux_agent_tools.toolkit.AgenTruxClient", mock_client_cls):
            toolkit = await AgenTruxToolkit.create()

        # Verify invite code flow was triggered (proves AGENTRUX_INVITE_CODE was read)
        mock_instance.redeem_grant.assert_called_once_with(
            invite_code="invite-xyz",
            script_id="script-123",
            client_secret="secret-abc",
        )
        # Verify get_token was called with the correct script_id and secret
        mock_instance.get_token.assert_called_once_with("script-123", "secret-abc")

        await toolkit.close()
