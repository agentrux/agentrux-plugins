"""Resource implementations for AgenTrux MCP Server."""
from __future__ import annotations

import base64
import json
import logging
import uuid
from typing import Any

from agentrux.sdk.facade import AgenTruxClient

from .tools import _envelope_to_dict, _validate_uuid

logger = logging.getLogger("agentrux.mcp.resources")


def _parse_jwt_scope(token: str) -> list[dict[str, str]]:
    """Parse JWT to extract accessible topics from the scope claim.

    The JWT scope contains entries like:
        "topic:<topic_id>:read"
        "topic:<topic_id>:write"
        "topic:<topic_id>:read+write"

    Returns a list of {"topic_id": str, "action": str} dicts.
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return []
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        logger.warning("Failed to decode JWT for scope parsing")
        return []

    scope_str = payload.get("scope", "")
    if not scope_str:
        return []

    topics: list[dict[str, str]] = []
    for entry in scope_str.split():
        # Expected format: "topic:<uuid>:<action>"
        parts = entry.split(":")
        if len(parts) != 3 or parts[0] != "topic":
            continue
        topic_id = parts[1]
        action = parts[2]
        # Validate the topic_id is a real UUID
        try:
            uuid.UUID(topic_id)
        except ValueError:
            continue
        topics.append({"topic_id": topic_id, "action": action})

    return topics


async def get_accessible_topics(client: AgenTruxClient) -> list[dict[str, str]]:
    """Return the list of topics accessible to the current script.

    Parses the JWT scope claim since there is no Data Plane API endpoint
    for listing topics.

    Returns:
        List of {"topic_id": str, "action": str} dicts.
    """
    token = client.api.token
    return _parse_jwt_scope(token)


async def get_topic_events(
    client: AgenTruxClient,
    topic_id: str,
    limit: int = 20,
) -> list[dict]:
    """Get recent events for a topic (resource representation).

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the topic.
        limit: Max events to return.

    Returns:
        List of event dicts.
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    envelopes, _ = await client.list_events(topic_id, limit=limit)
    return [_envelope_to_dict(e) for e in envelopes]
