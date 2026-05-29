"""Unit tests for AgenTruxToolkit and TOOL_DEFINITIONS."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agentrux_agent_tools.toolkit import TOOL_DEFINITIONS, AgenTruxToolkit


class _StubClient:
    """Minimal stand-in for the local ``AgentRuxClient`` (agentrux_agent_tools.client).

    ``toolkit.create()`` constructs the client directly (no ``from_*`` factory) and
    ``toolkit.close()`` awaits ``client.close()``, so the stub only needs an arg-tolerant
    constructor plus an async ``close``.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests for TOOL_DEFINITIONS
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    """Tests for the static TOOL_DEFINITIONS list."""

    EXPECTED_TOOL_NAMES = {
        "publish_event",
        "list_events",
        "get_event",
        "wait_for_event",
    }

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

    def test_anthropic_tool_names_match_openai(self, toolkit: AgenTruxToolkit) -> None:
        """Anthropic and OpenAI formats expose the same tool names."""
        openai_names = {t["function"]["name"] for t in toolkit.get_tools()}
        anthropic_names = {t["name"] for t in toolkit.get_tools_anthropic()}
        assert openai_names == anthropic_names


# ---------------------------------------------------------------------------
# Tests for AgenTruxToolkit.create() validation
# ---------------------------------------------------------------------------


class TestToolkitCreate:
    """Tests for AgenTruxToolkit.create() parameter validation."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear every AGENTRUX_* env var so a stray host setting can't
        flip an auth path on while we're asserting it stays off."""
        for var in (
            "AGENTRUX_BASE_URL",
            "AGENTRUX_CLIENT_ID",
            "AGENTRUX_CLIENT_SECRET",
            "AGENTRUX_ACCESS_TOKEN",
            "AGENTRUX_REFRESH_TOKEN",
            "AGENTRUX_OAUTH_CLIENT_ID",
            "AGENTRUX_ACTIVATION_CODE",
            "AGENTRUX_PROFILE",
        ):
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.asyncio
    async def test_create_raises_when_no_credentials_anywhere(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        """create() raises ValueError when none of the 4 auth paths can succeed.

        With every env var unset, no explicit args, and an empty
        credentials file, the toolkit must surface the device-flow hint
        rather than silently succeed.
        """
        # The toolkit's _CREDENTIALS_PATH is computed at import time, so
        # an env-var flip alone won't redirect it. Override the module
        # constant directly to point at an empty tmp_path.
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk

        monkeypatch.setattr(
            _tk,
            "_CREDENTIALS_PATH",
            _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_access_token_without_base_url(
        self,
    ) -> None:
        """Path 1 still validates base_url when an access_token is supplied."""
        with pytest.raises(ValueError, match="base_url is required"):
            await AgenTruxToolkit.create(access_token="AT-explicit")

    @pytest.mark.asyncio
    async def test_create_raises_when_only_client_id_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Path 3 (client_credentials) requires both client_id AND client_secret.

        Missing one half of the pair must NOT silently succeed; the
        toolkit falls through to Path 4 (profile load) and surfaces the
        device-flow hint when no credentials file exists.
        """
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk

        monkeypatch.setattr(
            _tk,
            "_CREDENTIALS_PATH",
            _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()

    @pytest.mark.asyncio
    async def test_create_raises_when_only_client_secret_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Path 3 (client_credentials) requires both client_id AND client_secret."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_CLIENT_ID", "crd_abc")
        from pathlib import Path as _Path
        import agentrux_agent_tools.toolkit as _tk

        monkeypatch.setattr(
            _tk,
            "_CREDENTIALS_PATH",
            _Path(tmp_path) / ".agentrux" / "credentials",
        )

        with pytest.raises(ValueError, match="No credentials found"):
            await AgenTruxToolkit.create()


# ---------------------------------------------------------------------------
# Tests for env var naming convention
# ---------------------------------------------------------------------------


class TestEnvVarNaming:
    """Verify the expected environment variable names are used."""

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "AGENTRUX_BASE_URL",
            "AGENTRUX_CLIENT_ID",
            "AGENTRUX_CLIENT_SECRET",
            "AGENTRUX_ACCESS_TOKEN",
            "AGENTRUX_REFRESH_TOKEN",
            "AGENTRUX_OAUTH_CLIENT_ID",
            "AGENTRUX_ACTIVATION_CODE",
            "AGENTRUX_PROFILE",
        ):
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.asyncio
    async def test_client_credentials_env_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create() reads AGENTRUX_BASE_URL / AGENTRUX_CLIENT_ID /
        AGENTRUX_CLIENT_SECRET and constructs AgentRuxClient with them (Path 2)."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_CLIENT_ID", "crd_abc")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "aks_secret")

        ctor = MagicMock(return_value=_StubClient())
        with patch("agentrux_agent_tools.toolkit.AgentRuxClient", ctor):
            toolkit = await AgenTruxToolkit.create()

        ctor.assert_called_once_with(
            base_url="https://api.example.com",
            client_id="crd_abc",
            client_secret="aks_secret",
        )
        await toolkit.close()

    @pytest.mark.asyncio
    async def test_access_token_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTRUX_ACCESS_TOKEN + AGENTRUX_BASE_URL constructs AgentRuxClient
        with token= (Path 1, no refresh wiring)."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_ACCESS_TOKEN", "AT-explicit")

        ctor = MagicMock(return_value=_StubClient())
        with patch("agentrux_agent_tools.toolkit.AgentRuxClient", ctor):
            toolkit = await AgenTruxToolkit.create()

        ctor.assert_called_once_with(
            base_url="https://api.example.com",
            token="AT-explicit",
        )
        await toolkit.close()
