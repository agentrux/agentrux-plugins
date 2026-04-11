"""MessageEnvelope - SDK internal message wrapper."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MessageEnvelope:
    """SDK internal message wrapper with sequence number for ordering."""

    event_id: str
    sequence_no: int
    timestamp: datetime
    type: str
    payload_mode: str
    payload: dict[str, Any] | None
    payload_ref: str | None
    producer_script: str
    received_at: float = field(default_factory=time.monotonic)

    @classmethod
    def from_api_response(cls, data: dict) -> MessageEnvelope:
        """Create from REST API response dict."""
        seq = data.get("sequence_no")
        if seq is None:
            from agentrux.sdk.errors import SequenceUnavailableError
            raise SequenceUnavailableError(
                f"sequence_no missing for event {data.get('event_id')}"
            )

        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        return cls(
            event_id=data["event_id"],
            sequence_no=int(seq),
            timestamp=ts,
            type=data.get("type", ""),
            payload_mode=data.get("payload_mode", "inline"),
            payload=data.get("payload"),
            payload_ref=data.get("payload_ref"),
            producer_script=data.get("producer_script", ""),
        )

    @classmethod
    def from_sse_event(cls, data: dict, sequence_no: int) -> MessageEnvelope:
        """Create from SSE event data with explicit sequence number."""
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)

        return cls(
            event_id=data.get("event_id", ""),
            sequence_no=sequence_no,
            timestamp=ts,
            type=data.get("type", ""),
            payload_mode=data.get("payload_mode", "inline"),
            payload=data.get("payload"),
            payload_ref=data.get("payload_ref"),
            producer_script=data.get("producer_script", ""),
        )

    def validate_event_id(self) -> bool:
        """Validate that event_id is a valid UUID."""
        try:
            uuid.UUID(self.event_id)
            return True
        except (ValueError, AttributeError):
            return False
