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


class _StubClient:
    """Minimal stub for AgenTruxClient used during import."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    async def close(self) -> None:
        pass


_fake_facade.AgenTruxClient = _StubClient  # type: ignore[attr-defined]

# Build the parent package path so Python resolves `agentrux.sdk.facade`
for mod_name in ("agentrux", "agentrux.sdk", "agentrux.sdk.facade"):
    if mod_name not in sys.modules:
        if mod_name == "agentrux.sdk.facade":
            sys.modules[mod_name] = _fake_facade
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
    async def test_create_raises_when_base_url_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() raises ValueError when base_url is not provided."""
        monkeypatch.delenv("AGENTRUX_BASE_URL", raising=False)
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)

        with pytest.raises(ValueError, match="base_url is required"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_script_id_and_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() raises ValueError when script_id/client_secret are missing."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)

        with pytest.raises(ValueError, match="script_id and client_secret are required"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_only_script_id_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() raises ValueError when only script_id is missing."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)

        with pytest.raises(ValueError, match="script_id and client_secret are required"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_only_client_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() raises ValueError when only client_secret is missing."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)

        with pytest.raises(ValueError, match="script_id and client_secret are required"):
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
            token="invite-xyz",
            script_id="script-123",
            client_secret="secret-abc",
        )
        # Verify get_token was called with the correct script_id and secret
        mock_instance.get_token.assert_called_once_with("script-123", "secret-abc")

        await toolkit.close()
