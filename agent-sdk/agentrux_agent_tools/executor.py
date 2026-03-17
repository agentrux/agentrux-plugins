"""Tool executor for AgenTrux agent tools.

Maps a tool name and arguments dict to the corresponding function in
:mod:`agentrux_agent_tools.tools` and returns the string result.

This module is designed to be used with *any* LLM framework's function
calling / tool_use mechanism.  The caller receives tool invocations from
the LLM, extracts ``name`` and ``arguments``, and passes them here.

Example::

    from agentrux_agent_tools.executor import execute_tool

    result = await execute_tool(client, "publish_event", {
        "topic_id": "...",
        "event_type": "chat.message",
        "payload": {"text": "hello"},
    })
"""
from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from agentrux.sdk.facade import AgenTruxClient

from . import tools

logger = logging.getLogger(__name__)

# Registry: tool name -> async callable(client, **kwargs) -> str
_TOOL_REGISTRY: dict[str, Any] = {
    "publish_event": tools.publish_event,
    "list_events": tools.list_events,
    "get_event": tools.get_event,
    "wait_for_event": tools.wait_for_event,
}


async def execute_tool(
    client: AgenTruxClient,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Execute a tool and return an LLM-readable string.

    Args:
        client: An authenticated AgenTruxClient.
        tool_name: Name of the tool to execute (must be in the registry).
        arguments: Keyword arguments parsed from the LLM tool call.

    Returns:
        A JSON string suitable for returning to the LLM as a tool result.

    Raises:
        ValueError: If the tool name is unknown.
    """
    fn = _TOOL_REGISTRY.get(tool_name)
    if fn is None:
        available = ", ".join(sorted(_TOOL_REGISTRY.keys()))
        raise ValueError(
            f"Unknown tool: {tool_name!r}. Available tools: {available}"
        )

    try:
        result = await fn(client, **arguments)
        return result
    except Exception as exc:
        logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
        return json.dumps(
            {
                "status": "error",
                "tool": tool_name,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
            indent=2,
        )
