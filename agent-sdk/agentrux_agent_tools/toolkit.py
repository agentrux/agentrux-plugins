"""AgenTrux Agent Toolkit.

Provides ``AgenTruxToolkit`` -- a framework-agnostic toolkit that exposes
AgenTrux operations as tool definitions compatible with OpenAI function
calling and Anthropic tool_use schemas.

Usage::

    toolkit = await AgenTruxToolkit.create()
    tools   = toolkit.get_tools()           # list[dict] for LLM
    result  = await toolkit.execute("publish_event", {...})

Auth resolution order (first match wins):
  1. Explicit ``access_token=`` argument (or ``AGENTRUX_ACCESS_TOKEN`` env);
     pair with ``refresh_token=`` + ``oauth_client_id=`` (or the
     ``AGENTRUX_REFRESH_TOKEN`` / ``AGENTRUX_OAUTH_CLIENT_ID`` env vars)
     to enable auto-refresh.
  2. Explicit ``script_id`` + ``client_secret`` (legacy client_credentials,
     also via ``AGENTRUX_SCRIPT_ID`` / ``AGENTRUX_CLIENT_SECRET`` env)
  3. ``~/.agentrux/credentials`` written by ``agentrux login`` (RFC 8628
     device flow). Profile defaults to "default", overridable via
     ``profile=`` arg or ``AGENTRUX_PROFILE`` env. Auto-refresh is
     wired in: when the SDK rotates the access/refresh token pair, the
     hook here re-acquires the per-profile lock and writes the new
     bundle back so the next process starts from the rotated token.

If none of those produce credentials, ``create()`` raises with a
human-readable hint pointing at ``agentrux login``.
"""
from __future__ import annotations

import configparser
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from agentrux.sdk.client import TokenBundle
from agentrux.sdk.facade import AgenTruxClient

from . import tools as tool_fns
from .cli import _profile_lock  # type: ignore[attr-defined]

logger = logging.getLogger(__name__)


_CREDENTIALS_PATH = Path.home() / ".agentrux" / "credentials"


def _load_cli_profile(profile: str) -> dict[str, str] | None:
    """Read tokens persisted by ``agentrux login`` for *profile*.

    Returns None if the file or the profile is missing — callers fall
    through to the legacy explicit-args path. Returning a tokens dict
    instead of asserting "expires_at > now" lets the underlying
    AgenTruxClient drive the refresh, which already knows how to swap
    access_token via the refresh_token grant.
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
        "script_id": sec.get("script_id", ""),
        "access_token": sec.get("access_token", ""),
        "refresh_token": sec.get("refresh_token", ""),
        "expires_at": sec.get("expires_at", "0"),
        "client_id": sec.get("client_id", ""),
    }
    if not out["access_token"]:
        return None
    return out


def _atomic_write_section(profile: str, updates: dict[str, str]) -> None:
    """Merge *updates* into the [*profile*] section and rewrite the file.

    Caller MUST hold ``_profile_lock(profile)`` so two writers can't
    interleave their merges. We re-read the file inside the lock so a
    sibling that updated a different profile in parallel doesn't lose
    its section to our overwrite.
    """
    cfg = configparser.ConfigParser()
    if _CREDENTIALS_PATH.exists():
        cfg.read(_CREDENTIALS_PATH)
    if profile not in cfg:
        cfg[profile] = {}
    for k, v in updates.items():
        cfg[profile][k] = v
    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=str(_CREDENTIALS_PATH.parent),
        prefix=".credentials.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        cfg.write(tmp)
        tmp_path = Path(tmp.name)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, _CREDENTIALS_PATH)


def _make_persist_hook(profile: str):
    """Build the SDK ``on_token_refreshed`` hook for *profile*.

    The hook re-acquires the per-profile lock, re-reads the credentials
    file (in case a sibling process already rotated past us), and writes
    the new bundle back atomically. Sync-by-design: the inner section is
    pure file-IO and does not block, so the SDK's ``await`` of a return
    value is unnecessary; we return None so the SDK skips the await.
    """

    def hook(bundle: TokenBundle) -> None:
        with _profile_lock(profile):
            _atomic_write_section(
                profile,
                {
                    "access_token": bundle.access_token,
                    "refresh_token": bundle.refresh_token,
                    "expires_at": str(bundle.expires_at),
                },
            )

    return hook

# ── Tool definitions in OpenAI function-calling format ──────────────────

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
                        "description": "UUID of the target topic.",
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
                        "description": "UUID of the topic to read from.",
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
                        "description": "UUID of the topic.",
                    },
                    "event_id": {
                        "type": "string",
                        "description": "UUID of the event to retrieve.",
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
                        "description": "UUID of the topic to watch.",
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

    The toolkit holds an authenticated ``AgenTruxClient`` and exposes tool
    definitions + an executor that maps tool calls to SDK operations.
    """

    def __init__(self, client: AgenTruxClient) -> None:
        self._client = client

    # ── Factory ──────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        base_url: str | None = None,
        script_id: str | None = None,
        client_secret: str | None = None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        oauth_client_id: str | None = None,
        profile: str | None = None,
        invite_code: str | None = None,
    ) -> AgenTruxToolkit:
        """Create an authenticated toolkit.

        Parameters fall back to environment variables, then to the
        device-flow credentials written by ``agentrux login``. See the
        module docstring for the resolution order.
        """
        base_url = base_url or os.environ.get("AGENTRUX_BASE_URL", "")
        script_id = script_id or os.environ.get("AGENTRUX_SCRIPT_ID", "")
        client_secret = client_secret or os.environ.get("AGENTRUX_CLIENT_SECRET", "")
        access_token = access_token or os.environ.get("AGENTRUX_ACCESS_TOKEN", "")
        refresh_token = refresh_token or os.environ.get("AGENTRUX_REFRESH_TOKEN", "")
        oauth_client_id = oauth_client_id or os.environ.get("AGENTRUX_OAUTH_CLIENT_ID", "")
        invite_code = invite_code or os.environ.get("AGENTRUX_INVITE_CODE")
        profile = profile or os.environ.get("AGENTRUX_PROFILE", "default")

        # Path 1: an explicit access_token wins outright. If the caller
        # also supplied refresh_token + oauth_client_id we wire up
        # auto-refresh; otherwise the caller is signalling they manage
        # rotation themselves and we leave refresh disabled.
        if access_token:
            if not base_url:
                raise ValueError(
                    "base_url is required when passing access_token. "
                    "Pass it directly or set AGENTRUX_BASE_URL."
                )
            client = AgenTruxClient(
                base_url=base_url,
                token=access_token,
                refresh_token=refresh_token or None,
                oauth_client_id=oauth_client_id or None,
            )
            logger.info("AgenTruxToolkit created from explicit access_token")
            return cls(client)

        # Path 2: legacy client_credentials. Used by CI / unattended
        # installs that issued a long-lived client_secret in Console.
        if script_id and client_secret:
            if not base_url:
                raise ValueError(
                    "base_url is required. Pass it directly or set AGENTRUX_BASE_URL."
                )
            temp_client = AgenTruxClient(base_url=base_url, token="")
            if invite_code:
                logger.info("Redeeming share code for script %s", script_id)
                await temp_client.redeem_grant(
                    invite_code=invite_code,
                    script_id=script_id,
                    client_secret=client_secret,
                )
            token_data = await temp_client.get_token(script_id, client_secret)
            await temp_client.close()
            client = AgenTruxClient(
                base_url=base_url,
                token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token"),
            )
            logger.info("AgenTruxToolkit created via client_credentials for script %s", script_id)
            return cls(client)

        # Path 3: device-flow profile from ``agentrux login``. We hold
        # the per-profile lock for the load+expiry-check region so a
        # concurrent refresh write-back can't race the load. The lock
        # is released before we hand control to AgenTruxClient because
        # the SDK will re-acquire it inside the persist hook on each
        # rotation; nesting the locks across an await would block any
        # sibling process for the toolkit's entire lifetime.
        with _profile_lock(profile):
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
            stored_client_id = creds.get("client_id", "") or None
            if stored_client_id is None and creds.get("refresh_token"):
                logger.warning(
                    "profile %s has a refresh_token but no client_id "
                    "— pre-OAuth-2.1 credentials. Auto-refresh disabled. "
                    "Re-run `agentrux login` to upgrade.", profile,
                )
            client = AgenTruxClient(
                base_url=chosen_base_url,
                token=creds["access_token"],
                refresh_token=creds["refresh_token"] or None,
                oauth_client_id=stored_client_id,
                on_token_refreshed=_make_persist_hook(profile),
            )
            logger.info(
                "AgenTruxToolkit created from %s [%s] for script %s",
                _CREDENTIALS_PATH, profile, creds["script_id"] or "(unknown)",
            )
            return cls(client)

        raise ValueError(
            "No credentials found. Either:\n"
            "  - run `agentrux login` to authenticate via OAuth device flow, OR\n"
            "  - set AGENTRUX_BASE_URL + AGENTRUX_SCRIPT_ID + AGENTRUX_CLIENT_SECRET, OR\n"
            "  - pass access_token=, script_id+client_secret=, or profile= directly."
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
    def client(self) -> AgenTruxClient:
        """Access the underlying AgenTruxClient."""
        return self._client

    async def close(self) -> None:
        """Release resources held by the underlying client."""
        await self._client.close()

    async def __aenter__(self) -> AgenTruxToolkit:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
