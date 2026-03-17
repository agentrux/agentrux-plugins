"""Configuration for AgenTrux MCP Server.

Reads connection parameters from environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MCPConfig:
    """Immutable configuration loaded from environment variables."""

    base_url: str
    script_id: str
    secret: str
    grant_token: str | None = None

    @classmethod
    def from_env(cls) -> MCPConfig:
        """Load configuration from environment variables.

        Required:
            AGENTRUX_BASE_URL: Base URL of the AgenTrux server
            AGENTRUX_SCRIPT_ID: Script ID for authentication
            AGENTRUX_SECRET: Script secret for authentication

        Optional:
            AGENTRUX_GRANT_TOKEN: Grant token for cross-account access
        """
        base_url = os.environ.get("AGENTRUX_BASE_URL", "")
        script_id = os.environ.get("AGENTRUX_SCRIPT_ID", "")
        secret = os.environ.get("AGENTRUX_SECRET", "")

        if not base_url:
            raise ValueError(
                "AGENTRUX_BASE_URL environment variable is required. "
                "Example: https://api.example.com"
            )
        if not script_id:
            raise ValueError(
                "AGENTRUX_SCRIPT_ID environment variable is required."
            )
        if not secret:
            raise ValueError(
                "AGENTRUX_SECRET environment variable is required."
            )

        return cls(
            base_url=base_url.rstrip("/"),
            script_id=script_id,
            secret=secret,
            grant_token=os.environ.get("AGENTRUX_GRANT_TOKEN"),
        )
