"""AgenTrux MCP Server.

Exposes AgenTrux Topic/Event operations as MCP tools and resources
so that LLMs can interact with the AgenTrux data plane.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource,
    TextContent,
    Tool,
)

from agentrux.sdk.facade import AgenTruxClient

from . import __version__
from .config import MCPConfig
from .resources import get_accessible_topics, get_topic_events
from .tools import (
    get_download_url,
    get_event,
    get_upload_url,
    list_events,
    publish_event,
)

logger = logging.getLogger("agentrux.mcp.server")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    Tool(
        name="publish_event",
        description=(
            "Publish an event to an AgenTrux topic. "
            "Requires write permission on the topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "string",
                    "description": "UUID of the target topic.",
                },
                "event_type": {
                    "type": "string",
                    "description": "Event type string (e.g. 'sensor.reading', 'task.completed').",
                },
                "payload": {
                    "type": "object",
                    "description": "Inline JSON payload. Optional if payload_ref is provided.",
                },
                "payload_ref": {
                    "type": "string",
                    "description": "Reference to a previously uploaded payload object_id. Optional.",
                },
            },
            "required": ["topic_id", "event_type"],
        },
    ),
    Tool(
        name="list_events",
        description=(
            "List events in an AgenTrux topic with pagination. "
            "Requires read permission on the topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "string",
                    "description": "UUID of the topic.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return (1-100, default 50).",
                    "default": 50,
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response.",
                },
                "event_type": {
                    "type": "string",
                    "description": "Filter events by type.",
                },
            },
            "required": ["topic_id"],
        },
    ),
    Tool(
        name="get_event",
        description=(
            "Get a single event by ID from an AgenTrux topic. "
            "Requires read permission on the topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "string",
                    "description": "UUID of the topic.",
                },
                "event_id": {
                    "type": "string",
                    "description": "UUID of the event.",
                },
            },
            "required": ["topic_id", "event_id"],
        },
    ),
    Tool(
        name="get_upload_url",
        description=(
            "Get a presigned upload URL for uploading a large payload to MinIO. "
            "Use this before publishing an event with payload_ref. "
            "Requires write permission on the topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "string",
                    "description": "UUID of the topic.",
                },
                "size": {
                    "type": "integer",
                    "description": "Payload size in bytes.",
                },
                "content_type": {
                    "type": "string",
                    "description": "MIME type (default: application/octet-stream).",
                    "default": "application/octet-stream",
                },
                "hash": {
                    "type": "string",
                    "description": "Optional SHA-256 hash of the payload for integrity verification.",
                },
            },
            "required": ["topic_id", "size"],
        },
    ),
    Tool(
        name="get_download_url",
        description=(
            "Get a presigned download URL for a payload stored in MinIO. "
            "Use the object_id from an event's payload_ref. "
            "Requires read permission on the topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "string",
                    "description": "UUID of the topic.",
                },
                "object_id": {
                    "type": "string",
                    "description": "UUID of the payload object.",
                },
            },
            "required": ["topic_id", "object_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------


def _create_server(config: MCPConfig) -> Server:
    """Create and configure the MCP server with tool/resource handlers."""
    server = Server("agentrux-mcp")
    _client: AgenTruxClient | None = None

    async def _get_client() -> AgenTruxClient:
        """Lazily authenticate and return the SDK client."""
        nonlocal _client
        if _client is not None:
            return _client

        logger.info("Authenticating with AgenTrux at %s ...", config.base_url)

        # Create a temporary client to call get_token
        tmp = AgenTruxClient(base_url=config.base_url, token="")

        # Redeem share code if provided (cross-account access)
        if config.invite_code:
            logger.info("Redeeming share code...")
            await tmp.redeem_grant(
                config.invite_code, config.script_id, config.client_secret
            )

        # Obtain JWT
        token_data = await tmp.get_token(config.script_id, config.client_secret)
        await tmp.close()

        _client = AgenTruxClient(
            base_url=config.base_url,
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
        )
        logger.info("Authenticated successfully.")
        return _client

    # -- Tool handlers --

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        client = await _get_client()
        try:
            if name == "publish_event":
                result = await publish_event(
                    client,
                    topic_id=arguments["topic_id"],
                    event_type=arguments["event_type"],
                    payload=arguments.get("payload"),
                    payload_ref=arguments.get("payload_ref"),
                )
            elif name == "list_events":
                result = await list_events(
                    client,
                    topic_id=arguments["topic_id"],
                    limit=arguments.get("limit", 50),
                    cursor=arguments.get("cursor"),
                    event_type=arguments.get("event_type"),
                )
            elif name == "get_event":
                result = await get_event(
                    client,
                    topic_id=arguments["topic_id"],
                    event_id=arguments["event_id"],
                )
            elif name == "get_upload_url":
                result = await get_upload_url(
                    client,
                    topic_id=arguments["topic_id"],
                    size=arguments["size"],
                    content_type=arguments.get("content_type", "application/octet-stream"),
                    hash=arguments.get("hash"),
                )
            elif name == "get_download_url":
                result = await get_download_url(
                    client,
                    topic_id=arguments["topic_id"],
                    object_id=arguments["object_id"],
                )
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [TextContent(type="text", text=json.dumps(result, indent=2))]
        except ValueError as e:
            return [TextContent(type="text", text=f"Validation error: {e}")]
        except Exception as e:
            logger.exception("Tool %s failed", name)
            return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]

    # -- Resource handlers --

    @server.list_resources()
    async def handle_list_resources() -> list[Resource]:
        return [
            Resource(
                uri="agentrux://accessible-topics",
                name="Accessible Topics",
                description=(
                    "List of topics this script has access to, "
                    "parsed from the JWT scope claim."
                ),
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def handle_read_resource(uri: str) -> str:
        client = await _get_client()
        uri_str = str(uri)

        if uri_str == "agentrux://accessible-topics":
            topics = await get_accessible_topics(client)
            return json.dumps(topics, indent=2)

        # Dynamic resource: agentrux://topics/{topic_id}/events
        if uri_str.startswith("agentrux://topics/") and uri_str.endswith("/events"):
            parts = uri_str.split("/")
            # agentrux://topics/<topic_id>/events -> parts[3] is topic_id
            if len(parts) == 5:
                topic_id = parts[3]
                events = await get_topic_events(client, topic_id)
                return json.dumps(events, indent=2)

        raise ValueError(f"Unknown resource: {uri_str}")

    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the AgenTrux MCP Server over stdio."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        config = MCPConfig.from_env()
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)

    logger.info("Starting AgenTrux MCP Server v%s", __version__)
    logger.info("Base URL: %s", config.base_url)

    server = _create_server(config)

    async def _run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(_run())


if __name__ == "__main__":
    main()
