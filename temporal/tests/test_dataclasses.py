"""Unit tests for Temporal plugin dataclasses."""
from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from temporal_agentrux.dataclasses import (
    EventData,
    GetEventInput,
    ListEventsInput,
    ListEventsResult,
    PublishInput,
    PublishResult,
    WaitInput,
    WaitResult,
)


class TestPublishInput:
    """Tests for PublishInput dataclass."""

    def test_creation_and_field_access(self) -> None:
        """PublishInput can be created and fields accessed."""
        inp = PublishInput(
            topic_id="topic-1",
            event_type="sensor.reading",
            payload={"temp": 22.5},
        )
        assert inp.topic_id == "topic-1"
        assert inp.event_type == "sensor.reading"
        assert inp.payload == {"temp": 22.5}

    def test_payload_defaults_to_none(self) -> None:
        """payload is optional and defaults to None."""
        inp = PublishInput(topic_id="t", event_type="e")
        assert inp.payload is None

    def test_is_dataclass(self) -> None:
        """PublishInput is a proper dataclass."""
        assert dataclasses.is_dataclass(PublishInput)


class TestPublishResult:
    """Tests for PublishResult dataclass."""

    def test_creation(self) -> None:
        result = PublishResult(event_id="evt-abc")
        assert result.event_id == "evt-abc"

    def test_serialization_via_asdict(self) -> None:
        """PublishResult can be serialized to a dict (Temporal payload converter compatibility)."""
        result = PublishResult(event_id="evt-123")
        d = dataclasses.asdict(result)
        assert d == {"event_id": "evt-123"}
        # Verify the dict is JSON-serializable (plain types only)
        import json

        json.dumps(d)  # should not raise


class TestListEventsInput:
    """Tests for ListEventsInput dataclass."""

    def test_defaults(self) -> None:
        """ListEventsInput has correct default values."""
        inp = ListEventsInput(topic_id="topic-1")
        assert inp.limit == 50
        assert inp.cursor is None
        assert inp.event_type is None

    def test_custom_values(self) -> None:
        inp = ListEventsInput(
            topic_id="t",
            limit=10,
            cursor="cur-abc",
            event_type="order.created",
        )
        assert inp.limit == 10
        assert inp.cursor == "cur-abc"
        assert inp.event_type == "order.created"


class TestGetEventInput:
    """Tests for GetEventInput dataclass."""

    def test_creation(self) -> None:
        inp = GetEventInput(topic_id="t1", event_id="e1")
        assert inp.topic_id == "t1"
        assert inp.event_id == "e1"


class TestWaitInput:
    """Tests for WaitInput dataclass."""

    def test_defaults(self) -> None:
        """WaitInput has correct default values."""
        inp = WaitInput(topic_id="topic-1")
        assert inp.timeout_seconds == 300.0
        assert inp.heartbeat_interval_seconds == 10.0
        assert inp.event_type is None

    def test_custom_values(self) -> None:
        inp = WaitInput(
            topic_id="t",
            event_type="chat.message",
            timeout_seconds=60.0,
            heartbeat_interval_seconds=5.0,
        )
        assert inp.timeout_seconds == 60.0
        assert inp.heartbeat_interval_seconds == 5.0
        assert inp.event_type == "chat.message"


class TestEventData:
    """Tests for EventData dataclass."""

    def test_creation(self) -> None:
        ed = EventData(
            event_id="e1",
            sequence_no=1,
            timestamp="2026-03-23T00:00:00Z",
            type="test.event",
            payload={"key": "value"},
            payload_ref=None,
            producer_script="script-1",
        )
        assert ed.event_id == "e1"
        assert ed.sequence_no == 1
        assert ed.type == "test.event"

    def test_serialization(self) -> None:
        """EventData can be serialized to dict."""
        ed = EventData(
            event_id="e1",
            sequence_no=1,
            timestamp="2026-03-23T00:00:00Z",
            type="t",
            payload=None,
            payload_ref="ref://bucket/key",
            producer_script="s1",
        )
        d = dataclasses.asdict(ed)
        assert d["payload_ref"] == "ref://bucket/key"
        assert d["payload"] is None


class TestListEventsResult:
    """Tests for ListEventsResult dataclass."""

    def test_defaults(self) -> None:
        result = ListEventsResult()
        assert result.events == []
        assert result.next_cursor is None

    def test_with_events(self) -> None:
        ed = EventData(
            event_id="e1",
            sequence_no=1,
            timestamp="2026-03-23T00:00:00Z",
            type="t",
            payload=None,
            payload_ref=None,
            producer_script="s1",
        )
        result = ListEventsResult(events=[ed], next_cursor="cur-next")
        assert len(result.events) == 1
        assert result.next_cursor == "cur-next"


class TestWaitResult:
    """Tests for WaitResult dataclass."""

    def test_defaults(self) -> None:
        result = WaitResult()
        assert result.found is False
        assert result.event is None

    def test_with_event(self) -> None:
        ed = EventData(
            event_id="e1",
            sequence_no=1,
            timestamp="2026-03-23T00:00:00Z",
            type="t",
            payload={"msg": "hello"},
            payload_ref=None,
            producer_script="s1",
        )
        result = WaitResult(found=True, event=ed)
        assert result.found is True
        assert result.event is not None
        assert result.event.event_id == "e1"
