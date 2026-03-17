"""Temporal activity implementations for AgenTrux.

All activities use a shared AgenTruxClient initialized at worker startup.
Secrets are read from environment variables, never from activity inputs.
"""

from __future__ import annotations

import asyncio
import logging
import time

from temporalio import activity

from .dataclasses import (
    EventData,
    GetEventInput,
    ListEventsInput,
    ListEventsResult,
    PublishInput,
    PublishResult,
    WaitInput,
    WaitResult,
)

logger = logging.getLogger("temporal_agentrux.activities")


def _envelope_to_data(envelope) -> EventData:
    """Convert a MessageEnvelope to a serializable EventData."""
    return EventData(
        event_id=envelope.event_id,
        sequence_no=envelope.sequence_no,
        timestamp=envelope.timestamp.isoformat() if envelope.timestamp else "",
        type=envelope.type,
        payload=envelope.payload,
        payload_ref=envelope.payload_ref,
        producer_script=envelope.producer_script,
    )


@activity.defn
async def publish_event(input: PublishInput) -> PublishResult:
    """Publish an event to an AgenTrux topic.

    Returns the event_id of the published event.
    """
    from .worker import get_client

    client = get_client()
    event_id = await client.publish(
        topic_id=input.topic_id,
        event_type=input.event_type,
        payload=input.payload,
    )
    logger.info("Published event %s to topic %s", event_id, input.topic_id)
    return PublishResult(event_id=event_id)


@activity.defn
async def list_events_activity(input: ListEventsInput) -> ListEventsResult:
    """List events from an AgenTrux topic with optional filtering."""
    from .worker import get_client

    client = get_client()
    envelopes, next_cursor = await client.list_events(
        topic_id=input.topic_id,
        limit=input.limit,
        cursor=input.cursor,
        event_type=input.event_type,
    )
    events = [_envelope_to_data(e) for e in envelopes]
    return ListEventsResult(events=events, next_cursor=next_cursor)


@activity.defn
async def get_event_activity(input: GetEventInput) -> dict:
    """Get a single event by ID from an AgenTrux topic.

    Returns the event as a plain dict for maximum flexibility.
    """
    from .worker import get_client

    client = get_client()
    envelope = await client.get_event(
        topic_id=input.topic_id,
        event_id=input.event_id,
    )
    data = _envelope_to_data(envelope)
    return {
        "event_id": data.event_id,
        "sequence_no": data.sequence_no,
        "timestamp": data.timestamp,
        "type": data.type,
        "payload": data.payload,
        "payload_ref": data.payload_ref,
        "producer_script": data.producer_script,
    }


@activity.defn
async def wait_for_event(input: WaitInput) -> WaitResult:
    """Wait for a matching event on an AgenTrux topic via SSE subscription.

    Sends periodic heartbeats to Temporal to prevent activity timeout.
    Returns when a matching event arrives or the timeout is reached.
    """
    from .worker import get_client

    client = get_client()
    deadline = time.monotonic() + input.timeout_seconds
    last_heartbeat = time.monotonic()

    sub = client.subscribe(input.topic_id, mode="sse")
    try:
        async for envelope in sub:
            # Check if the event matches the filter
            if input.event_type is not None and envelope.type != input.event_type:
                pass  # Skip non-matching events
            else:
                return WaitResult(
                    found=True,
                    event=_envelope_to_data(envelope),
                )

            # Send heartbeat periodically
            now = time.monotonic()
            if now - last_heartbeat >= input.heartbeat_interval_seconds:
                activity.heartbeat(f"waiting on {input.topic_id}")
                last_heartbeat = now

            # Check timeout
            if now >= deadline:
                logger.info(
                    "Timeout reached waiting for event on topic %s",
                    input.topic_id,
                )
                return WaitResult(found=False, event=None)

            # Check for cancellation
            if activity.is_cancelled():
                logger.info("Activity cancelled while waiting on topic %s", input.topic_id)
                return WaitResult(found=False, event=None)

    except asyncio.CancelledError:
        logger.info("Activity cancelled (CancelledError) on topic %s", input.topic_id)
        return WaitResult(found=False, event=None)
    finally:
        await sub.unsubscribe()

    return WaitResult(found=False, event=None)
