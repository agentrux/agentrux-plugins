"""AgenTrux Agent Toolkit.

Provides ``AgenTruxToolkit`` -- a framework-agnostic toolkit that exposes
AgenTrux operations as tool definitions compatible with OpenAI function
calling and Anthropic tool_use schemas.

Usage::

    toolkit = await AgenTruxToolkit.create()
    tools   = toolkit.get_tools()           # list[dict] for LLM
    result  = await toolkit.execute("publish_event", {...})

Auth resolution order (first match wins):
  1. Explicit ``access_token=`` argument (or ``AGENTRUX_ACCESS_TOKEN`` env).
     The caller manages rotation; the toolkit just attaches the Bearer.
  2. Explicit ``client_id`` + ``client_secret`` (OAuth 2.1 client_credentials,
     also via ``AGENTRUX_CLIENT_ID`` / ``AGENTRUX_CLIENT_SECRET`` env).
     ``client_id`` is the ``crd_<uuid>`` issued by
     ``POST /auth/redeem-activation-code``; ``client_secret`` is ``aks_<plain>``.
     The toolkit issues an aat_ Bearer via ``POST /oauth/token``.
  3. ``~/.agentrux/credentials`` written by ``agentrux login`` (RFC 8628
     device flow). Profile defaults to "default", overridable via
     ``profile=`` arg or ``AGENTRUX_PROFILE`` env. The toolkit reads
     access_token + refresh_token straight out of the file and rotates
     them on expiry via the refresh_token grant.

If none of those produce credentials, ``create()`` raises with a
human-readable hint pointing at ``agentrux login``.

Phase 1.9+ note: the legacy ``script_id`` + ``client_secret`` pair (used by
the old ``/auth/token`` endpoint that is gone) is not supported. Pass
``client_id`` / ``AGENTRUX_CLIENT_ID`` instead — that's the ``crd_<uuid>``
the activation-code redemption now returns.
"""
from __future__ import annotations

import configparser
import logging
import os
import time
from pathlib import Path
from typing import Any

from .client import AgentRuxClient

from . import tools as tool_fns

logger = logging.getLogger(__name__)


_CREDENTIALS_PATH = Path.home() / ".agentrux" / "credentials"


def _load_cli_profile(profile: str) -> dict[str, str] | None:
    """Read tokens persisted by ``agentrux login`` for *profile*.

    Returns None if the file or the profile is missing — callers fall
    through to the explicit-args path. Returning a tokens dict instead of
    asserting "expires_at > now" lets the underlying AgentRuxClient drive
    the refresh, which already knows how to swap access_token via the
    refresh_token grant.

    The credentials file is written by ``cli.py``; we look up both the
    new ``client_id`` field and the legacy ``script_id`` field so old
    profiles (written before Phase 1.9) still resolve to *something*
    sensible, even if rotation will eventually require a re-login.
    """
    if not _CREDENTIALS_PATH.exists():
        return None
    cfg = configparser.ConfigParser()
    cfg.read(_CREDENTIALS_PATH)
    if profile not in cfg:
        return None
    sec = cfg[profile]
    out = {
        "base_url": sec.get("base_url", ""),
        "client_id": sec.get("client_id", "") or sec.get("script_id", ""),
        "access_token": sec.get("access_token", ""),
        "refresh_token": sec.get("refresh_token", ""),
        "expires_at": sec.get("expires_at", "0"),
    }
    if not out["access_token"]:
        return None
    return out

# ── Tool definitions in OpenAI function-calling format ──────────────────

_TOPIC_ID_DESC = (
    "AgenTrux Topic ID with the `top_` prefix (e.g. 'top_019d32da-...'). "
    "Bare UUIDs are rejected — the server-side data-plane enforces the prefix."
)
_EVENT_ID_DESC = (
    "AgenTrux Event ID with the `evt_` prefix (e.g. 'evt_019d32db-...')."
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "publish_event",
            "description": (
                "Publish an event to an AgenTrux topic. "
                "Returns the event_id of the newly created event."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": f"Target topic. {_TOPIC_ID_DESC}",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Dot-separated event type (e.g. 'sensor.reading').",
                    },
                    "payload": {
                        "type": "object",
                        "description": "Arbitrary JSON payload to include in the event.",
                    },
                },
                "required": ["topic_id", "event_type", "payload"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_events",
            "description": (
                "List recent events from an AgenTrux topic. "
                "Returns a JSON array of events with event_id, type, timestamp, and payload."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": f"Topic to read from. {_TOPIC_ID_DESC}",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of events to return (default 20).",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Optional event type filter.",
                    },
                },
                "required": ["topic_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event",
            "description": (
                "Retrieve a single event by its event_id from an AgenTrux topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": _TOPIC_ID_DESC,
                    },
                    "event_id": {
                        "type": "string",
                        "description": _EVENT_ID_DESC,
                    },
                },
                "required": ["topic_id", "event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_for_event",
            "description": (
                "Wait for the next event on an AgenTrux topic via SSE. "
                "Blocks until an event arrives or the timeout is reached. "
                "Returns the event data or a timeout message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic_id": {
                        "type": "string",
                        "description": f"Topic to watch. {_TOPIC_ID_DESC}",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum seconds to wait (default 30).",
                    },
                    "event_type": {
                        "type": "string",
                        "description": "Optional event type filter (client-side).",
                    },
                },
                "required": ["topic_id"],
            },
        },
    },
]


class AgenTruxToolkit:
    """Framework-agnostic AI agent toolkit for AgenTrux.

    The toolkit holds an authenticated ``AgentRuxClient`` and exposes tool
    definitions + an executor that maps tool calls to SDK operations.
    """

    def __init__(self, client: AgentRuxClient) -> None:
        self._client = client

    # ── Factory ──────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        profile: str | None = None,
    ) -> AgenTruxToolkit:
        """Create an authenticated toolkit.

        Parameters fall back to environment variables, then to the
        device-flow credentials written by ``agentrux login``. See the
        module docstring for the resolution order.

        ``client_id`` is the ``crd_<uuid>`` that
        ``POST /auth/redeem-activation-code`` returns; ``client_secret``
        is ``aks_<plain>`` from the same response.
        """
        base_url = base_url or os.environ.get("AGENTRUX_BASE_URL", "")
        client_id = client_id or os.environ.get("AGENTRUX_CLIENT_ID", "")
        client_secret = client_secret or os.environ.get("AGENTRUX_CLIENT_SECRET", "")
        access_token = access_token or os.environ.get("AGENTRUX_ACCESS_TOKEN", "")
        profile = profile or os.environ.get("AGENTRUX_PROFILE", "default")

        # Path 1: an explicit access_token wins outright. No refresh
        # token is wired in this path because the caller is signalling
        # they manage rotation themselves.
        if access_token:
            if not base_url:
                raise ValueError(
                    "base_url is required when passing access_token. "
                    "Pass it directly or set AGENTRUX_BASE_URL."
                )
            client = AgentRuxClient(base_url=base_url, token=access_token)
            logger.info("AgenTruxToolkit created from explicit access_token")
            return cls(client)

        # Path 2: OAuth 2.1 client_credentials (Phase 1.9+).
        # The plugin issues /oauth/token grant_type=client_credentials
        # lazily on first request via AgentRuxClient._ensure_token().
        if client_id and client_secret:
            if not base_url:
                raise ValueError(
                    "base_url is required. Pass it directly or set AGENTRUX_BASE_URL."
                )
            client = AgentRuxClient(
                base_url=base_url,
                client_id=client_id,
                client_secret=client_secret,
            )
            logger.info("AgenTruxToolkit created via client_credentials for client_id %s", client_id)
            return cls(client)

        # Path 3: device-flow profile from ``agentrux login``.
        creds = _load_cli_profile(profile)
        if creds is not None:
            # The CLI persisted a Bearer access_token + a refresh_token.
            # Use the persisted base_url unless the caller overrode it.
            chosen_base_url = base_url or creds["base_url"]
            if not chosen_base_url:
                raise ValueError(
                    f"profile {profile!r} in {_CREDENTIALS_PATH} has no base_url"
                )
            try:
                expired = int(creds.get("expires_at") or 0) <= int(time.time())
            except ValueError:
                expired = False
            if expired:
                logger.info(
                    "AgenTruxToolkit: profile %s access_token expired, "
                    "client will refresh via the saved refresh_token", profile,
                )
            client = AgentRuxClient(
                base_url=chosen_base_url,
                token=creds["access_token"],
                refresh_token=creds["refresh_token"] or None,
                client_id_for_refresh=creds["client_id"],
            )
            logger.info(
                "AgenTruxToolkit created from %s [%s] for client %s",
                _CREDENTIALS_PATH, profile, creds["client_id"] or "(unknown)",
            )
            return cls(client)

        raise ValueError(
            "No credentials found. Either:\n"
            "  - run `agentrux login` to authenticate via OAuth device flow, OR\n"
            "  - set AGENTRUX_BASE_URL + AGENTRUX_CLIENT_ID + AGENTRUX_CLIENT_SECRET, OR\n"
            "  - pass access_token=, client_id+client_secret=, or profile= directly."
        )

    # ── Tool definitions ─────────────────────────────────────────────────

    def get_tools(self) -> list[dict[str, Any]]:
        """Return tool definitions in OpenAI function-calling format.

        Each item has ``{"type": "function", "function": {...}}``.
        Compatible with OpenAI, Anthropic (after conversion), LangChain, etc.
        """
        return TOOL_DEFINITIONS

    def get_tools_anthropic(self) -> list[dict[str, Any]]:
        """Return tool definitions in Anthropic tool_use format.

        Each item has ``{"name": str, "description": str, "input_schema": {...}}``.
        """
        return [
            {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "input_schema": t["function"]["parameters"],
            }
            for t in TOOL_DEFINITIONS
        ]

    # ── Execution ────────────────────────────────────────────────────────

    async def execute(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool by name and return an LLM-readable string result.

        Delegates to :mod:`agentrux_agent_tools.tools`.
        """
        from .executor import execute_tool

        return await execute_tool(self._client, tool_name, arguments)

    # ── Lifecycle ────────────────────────────────────────────────────────

    @property
    def client(self) -> AgentRuxClient:
        """Access the underlying AgentRuxClient."""
        return self._client

    async def close(self) -> None:
        """Release resources held by the underlying client."""
        await self._client.close()

    async def __aenter__(self) -> AgenTruxToolkit:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
