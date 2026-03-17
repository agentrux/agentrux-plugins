"""Input/output dataclasses for Temporal activities.

All dataclasses must be JSON-serializable for Temporal's payload converter.
Secrets are never passed as activity arguments — they are read from
environment variables at worker initialization time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PublishInput:
    """Input for publish_event activity."""

    topic_id: str
    event_type: str
    payload: dict[str, Any] | None = None


@dataclass
class PublishResult:
    """Output for publish_event activity."""

    event_id: str


@dataclass
class ListEventsInput:
    """Input for list_events_activity."""

    topic_id: str
    limit: int = 50
    cursor: str | None = None
    event_type: str | None = None


@dataclass
class EventData:
    """Serializable representation of a MessageEnvelope."""

    event_id: str
    sequence_no: int
    timestamp: str
    type: str
    payload: dict[str, Any] | None
    payload_ref: str | None
    producer_script: str


@dataclass
class ListEventsResult:
    """Output for list_events_activity."""

    events: list[EventData] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass
class GetEventInput:
    """Input for get_event_activity."""

    topic_id: str
    event_id: str


@dataclass
class WaitInput:
    """Input for wait_for_event activity.

    Subscribes via SSE and waits until an event matching the filter
    arrives or the timeout is reached.
    """

    topic_id: str
    event_type: str | None = None
    timeout_seconds: float = 300.0
    heartbeat_interval_seconds: float = 10.0


@dataclass
class WaitResult:
    """Output for wait_for_event activity."""

    found: bool = False
    event: EventData | None = None
