"""Individual tool functions for AgenTrux agent integration.

Each function accepts an ``AgenTruxClient`` and keyword arguments that
match the tool parameter schemas defined in :mod:`toolkit`.  All functions
return an LLM-readable JSON string.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from agentrux.sdk.facade import AgenTruxClient

logger = logging.getLogger(__name__)


def _envelope_to_dict(env: Any) -> dict[str, Any]:
    """Convert a MessageEnvelope to a plain dict for JSON serialisation."""
    return {
        "event_id": env.event_id,
        "sequence_no": env.sequence_no,
        "timestamp": env.timestamp.isoformat() if env.timestamp else None,
        "type": env.type,
        "payload": env.payload,
        "payload_ref": env.payload_ref,
        "producer_script": env.producer_script,
    }


async def publish_event(
    client: AgenTruxClient,
    *,
    topic_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Publish an event and return the event_id as JSON."""
    event_id = await client.publish(
        topic_id=topic_id,
        event_type=event_type,
        payload=payload,
    )
    return json.dumps(
        {
            "status": "published",
            "event_id": event_id,
            "topic_id": topic_id,
            "event_type": event_type,
        },
        indent=2,
    )


async def list_events(
    client: AgenTruxClient,
    *,
    topic_id: str,
    limit: int = 20,
    event_type: str | None = None,
) -> str:
    """List events and return them as a JSON array."""
    kwargs: dict[str, Any] = {"limit": limit}
    if event_type:
        kwargs["event_type"] = event_type

    envelopes, _cursor = await client.list_events(topic_id=topic_id, **kwargs)

    events = [_envelope_to_dict(env) for env in envelopes]
    return json.dumps(
        {
            "topic_id": topic_id,
            "count": len(events),
            "events": events,
        },
        indent=2,
    )


async def get_event(
    client: AgenTruxClient,
    *,
    topic_id: str,
    event_id: str,
) -> str:
    """Retrieve a single event and return it as JSON."""
    env = await client.get_event(topic_id=topic_id, event_id=event_id)
    return json.dumps(_envelope_to_dict(env), indent=2)


async def wait_for_event(
    client: AgenTruxClient,
    *,
    topic_id: str,
    timeout_seconds: int = 30,
    event_type: str | None = None,
) -> str:
    """Wait for the next event via SSE and return it as JSON.

    If no event arrives within *timeout_seconds*, returns a timeout message.
    """
    result: dict[str, Any] | None = None

    sub = client.subscribe(topic_id=topic_id, mode="sse")

    async def _wait() -> None:
        nonlocal result
        async with sub:
            async for env in sub:
                if event_type and env.type != event_type:
                    continue
                result = _envelope_to_dict(env)
                break

    try:
        await asyncio.wait_for(_wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return json.dumps(
            {
                "status": "timeout",
                "topic_id": topic_id,
                "timeout_seconds": timeout_seconds,
                "message": f"No event received within {timeout_seconds} seconds.",
            },
            indent=2,
        )

    return json.dumps(
        {
            "status": "received",
            "topic_id": topic_id,
            "event": result,
        },
        indent=2,
    )
