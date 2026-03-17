"""Tool implementations for AgenTrux MCP Server."""
from __future__ import annotations

import json
import uuid
from typing import Any

from agentrux.sdk.facade import AgenTruxClient


def _validate_uuid(value: str, name: str) -> str:
    """Validate that a string is a valid UUID. Returns the normalized string."""
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ValueError(f"{name} must be a valid UUID, got: {value!r}")


def _envelope_to_dict(envelope: Any) -> dict:
    """Convert a MessageEnvelope to a JSON-serializable dict."""
    return {
        "event_id": envelope.event_id,
        "sequence_no": envelope.sequence_no,
        "timestamp": envelope.timestamp.isoformat() if envelope.timestamp else None,
        "type": envelope.type,
        "payload": envelope.payload,
        "payload_ref": envelope.payload_ref,
        "producer_script": envelope.producer_script,
    }


async def publish_event(
    client: AgenTruxClient,
    topic_id: str,
    event_type: str,
    payload: dict | None = None,
    payload_ref: str | None = None,
) -> dict:
    """Publish an event to a topic.

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the target topic.
        event_type: Event type string (e.g. "sensor.reading").
        payload: Inline JSON payload (optional if payload_ref is set).
        payload_ref: Reference to a previously uploaded payload object (optional).

    Returns:
        {"event_id": str} on success.
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    event_id = await client.publish(
        topic_id, event_type, payload, payload_ref=payload_ref
    )
    return {"event_id": event_id}


async def list_events(
    client: AgenTruxClient,
    topic_id: str,
    limit: int = 50,
    cursor: str | None = None,
    event_type: str | None = None,
) -> dict:
    """List events in a topic.

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the topic.
        limit: Max events to return (1-100, default 50).
        cursor: Pagination cursor from previous response.
        event_type: Filter by event type.

    Returns:
        {"events": [...], "next_cursor": str | null}
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    limit = max(1, min(100, limit))

    envelopes, next_cursor = await client.list_events(
        topic_id, limit=limit, cursor=cursor, event_type=event_type
    )
    return {
        "events": [_envelope_to_dict(e) for e in envelopes],
        "next_cursor": next_cursor,
    }


async def get_event(
    client: AgenTruxClient,
    topic_id: str,
    event_id: str,
) -> dict:
    """Get a single event by ID.

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the topic.
        event_id: UUID of the event.

    Returns:
        Event data dict.
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    event_id = _validate_uuid(event_id, "event_id")

    envelope = await client.get_event(topic_id, event_id)
    return _envelope_to_dict(envelope)


async def get_upload_url(
    client: AgenTruxClient,
    topic_id: str,
    size: int,
    content_type: str = "application/octet-stream",
    hash: str | None = None,
) -> dict:
    """Get a presigned upload URL for a large payload.

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the topic.
        size: Payload size in bytes.
        content_type: MIME type of the payload.
        hash: Optional SHA-256 hash of the payload.

    Returns:
        {"object_id": str, "upload_url": str, "expiration": str}
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    body: dict[str, Any] = {"size": size, "content_type": content_type}
    if hash is not None:
        body["hash"] = hash

    # TODO: Replace with facade method when AgenTruxClient adds
    # get_upload_url() / get_download_url() to the public API.
    # Currently uses internal _request() as no public method exists yet.
    resp = await client.api._request(
        "POST", f"/topics/{topic_id}/payloads", json=body
    )
    return resp.json()


async def get_download_url(
    client: AgenTruxClient,
    topic_id: str,
    object_id: str,
) -> dict:
    """Get a presigned download URL for a payload.

    Args:
        client: Authenticated AgenTruxClient.
        topic_id: UUID of the topic.
        object_id: UUID of the payload object.

    Returns:
        {"object_id": str, "content_type": str, "size": int, "download_url": str, "expiration": str}
    """
    topic_id = _validate_uuid(topic_id, "topic_id")
    object_id = _validate_uuid(object_id, "object_id")

    # TODO: Same as get_upload_url — replace with facade method when available.
    resp = await client.api._request(
        "GET", f"/topics/{topic_id}/payloads/{object_id}"
    )
    return resp.json()
