"""AgenTrux Agent Toolkit.

Provides ``AgenTruxToolkit`` -- a framework-agnostic toolkit that exposes
AgenTrux operations as tool definitions compatible with OpenAI function
calling and Anthropic tool_use schemas.

Usage::

    toolkit = await AgenTruxToolkit.create()
    tools   = toolkit.get_tools()           # list[dict] for LLM
    result  = await toolkit.execute("publish_event", {...})
"""
from __future__ import annotations

import logging
import os
from typing import Any

from agentrux.sdk.facade import AgenTruxClient

from . import tools as tool_fns

logger = logging.getLogger(__name__)

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
        invite_code: str | None = None,
    ) -> AgenTruxToolkit:
        """Create an authenticated toolkit.

        Parameters fall back to environment variables:

        - ``AGENTRUX_BASE_URL``
        - ``AGENTRUX_SCRIPT_ID``
        - ``AGENTRUX_CLIENT_SECRET``
        - ``AGENTRUX_INVITE_CODE`` (optional)
        """
        base_url = base_url or os.environ.get("AGENTRUX_BASE_URL", "")
        script_id = script_id or os.environ.get("AGENTRUX_SCRIPT_ID", "")
        client_secret = client_secret or os.environ.get("AGENTRUX_CLIENT_SECRET", "")
        invite_code = invite_code or os.environ.get("AGENTRUX_INVITE_CODE")

        if not base_url:
            raise ValueError(
                "base_url is required. Pass it directly or set AGENTRUX_BASE_URL."
            )
        if not script_id or not client_secret:
            raise ValueError(
                "script_id and client_secret are required. Pass them directly or set "
                "AGENTRUX_SCRIPT_ID / AGENTRUX_CLIENT_SECRET."
            )

        # Create a temporary unauthenticated client for the auth endpoints
        temp_client = AgenTruxClient(base_url=base_url, token="")

        # Redeem share code if provided
        if invite_code:
            logger.info("Redeeming share code for script %s", script_id)
            await temp_client.redeem_grant(
                invite_code=invite_code,
                script_id=script_id,
                client_secret=client_secret,
            )

        # Obtain JWT
        token_data = await temp_client.get_token(script_id, client_secret)
        await temp_client.close()

        client = AgenTruxClient(
            base_url=base_url,
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
        )

        logger.info("AgenTruxToolkit created for script %s", script_id)
        return cls(client)

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
