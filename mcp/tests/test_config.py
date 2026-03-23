"""Unit tests for MCPConfig.from_env()."""
from __future__ import annotations

import pytest

from agentrux_mcp.config import MCPConfig


class TestMCPConfigFromEnv:
    """Tests for MCPConfig.from_env() environment variable loading."""

    def test_successful_config_creation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All required env vars set produces a valid MCPConfig."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        monkeypatch.setenv("AGENTRUX_INVITE_CODE", "invite-xyz")

        cfg = MCPConfig.from_env()

        assert cfg.base_url == "https://api.example.com"
        assert cfg.script_id == "script-123"
        assert cfg.client_secret == "secret-abc"
        assert cfg.invite_code == "invite-xyz"

    def test_missing_base_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing AGENTRUX_BASE_URL raises ValueError."""
        monkeypatch.delenv("AGENTRUX_BASE_URL", raising=False)
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")

        with pytest.raises(ValueError, match="AGENTRUX_BASE_URL"):
            MCPConfig.from_env()

    def test_missing_script_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing AGENTRUX_SCRIPT_ID raises ValueError."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.delenv("AGENTRUX_SCRIPT_ID", raising=False)
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")

        with pytest.raises(ValueError, match="AGENTRUX_SCRIPT_ID"):
            MCPConfig.from_env()

    def test_missing_client_secret_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing AGENTRUX_CLIENT_SECRET raises ValueError."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.delenv("AGENTRUX_CLIENT_SECRET", raising=False)

        with pytest.raises(ValueError, match="AGENTRUX_CLIENT_SECRET"):
            MCPConfig.from_env()

    def test_invite_code_is_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTRUX_INVITE_CODE is optional and defaults to None."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")
        monkeypatch.delenv("AGENTRUX_INVITE_CODE", raising=False)

        cfg = MCPConfig.from_env()

        assert cfg.invite_code is None

    def test_trailing_slash_stripped_from_base_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trailing slashes on base_url are stripped."""
        monkeypatch.setenv("AGENTRUX_BASE_URL", "https://api.example.com///")
        monkeypatch.setenv("AGENTRUX_SCRIPT_ID", "script-123")
        monkeypatch.setenv("AGENTRUX_CLIENT_SECRET", "secret-abc")

        cfg = MCPConfig.from_env()

        assert cfg.base_url == "https://api.example.com"

    def test_field_names_use_unified_token_naming(self) -> None:
        """Config uses 'client_secret' and 'invite_code' (not 'secret' or 'grant_token')."""
        cfg = MCPConfig(
            base_url="https://example.com",
            script_id="s1",
            client_secret="cs",
            invite_code="ic",
        )
        # Field access by the canonical names must work
        assert cfg.client_secret == "cs"
        assert cfg.invite_code == "ic"

        # Verify these are the actual dataclass field names
        field_names = {f.name for f in cfg.__dataclass_fields__.values()}
        assert "client_secret" in field_names
        assert "invite_code" in field_names
        # Old names must NOT exist
        assert "secret" not in field_names
        assert "grant_token" not in field_names

    def test_config_is_frozen(self) -> None:
        """MCPConfig instances are immutable (frozen dataclass)."""
        cfg = MCPConfig(
            base_url="https://example.com",
            script_id="s1",
            client_secret="cs",
        )
        with pytest.raises(AttributeError):
            cfg.base_url = "https://other.com"  # type: ignore[misc]
