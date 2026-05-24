"""Tests for envelope.py: MessageEnvelope / PublishResult / ListEventsPage.

Coverage axes per memory `feedback_tests_max_strictness`:
  a) normal       — typical inputs round-trip through dataclasses
  b) errors       — server returns malformed / missing fields
  c) boundary     — extreme but valid values (empty metadata, large seq #)
  d) attack       — prefix tampering, unexpected types in nullable slots
"""
from __future__ import annotations

import pytest

from agentrux.sdk.envelope import (
    ListEventsPage,
    MessageEnvelope,
    PageCursor,
    PublishResult,
    TopicCursorState,
)

from .conftest import stub_event_view, stub_list_events_response, stub_publish_response


# ---------------------------------------------------------------------------
# (a) normal — MessageEnvelope
# ---------------------------------------------------------------------------


def test_envelope_normal_inline() -> None:
    env = MessageEnvelope.from_event_view(
        stub_event_view(payload={"k": "v"}, metadata={"trace": "abc"})
    )
    assert env.event_id.startswith("evt_")
    assert env.topic_id.startswith("top_")
    assert env.producer_script_id.startswith("scr_")
    assert env.payload_kind == "inline"
    assert env.payload == {"k": "v"}
    assert env.metadata == {"trace": "abc"}
    assert env.payload_object_id is None


def test_envelope_normal_object_ref() -> None:
    env = MessageEnvelope.from_event_view(
        stub_event_view(
            payload_kind="object_ref",
            payload=None,
            payload_object_id="pob_00000000-0000-0000-0000-000000000001",
            metadata={"size": 1024},
        )
    )
    assert env.payload_kind == "object_ref"
    assert env.payload is None
    assert env.payload_object_id.startswith("pob_")
    assert env.metadata == {"size": 1024}


def test_envelope_with_expand_schema() -> None:
    raw = stub_event_view()
    raw["schema"] = {"id": "tsh_xyz", "version": 1}
    env = MessageEnvelope.from_event_view(raw)
    assert env.schema == {"id": "tsh_xyz", "version": 1}


# ---------------------------------------------------------------------------
# (b) errors — required fields missing / wrong types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing_field",
    [
        "event_id", "topic_id", "sequence_number", "payload_kind",
        "producer_script_id", "stored_at", "ttl_expires_at",
    ],
)
def test_envelope_missing_required_field_raises(missing_field: str) -> None:
    raw = stub_event_view()
    raw.pop(missing_field)
    with pytest.raises(ValueError, match="missing required field"):
        MessageEnvelope.from_event_view(raw)


def test_envelope_unknown_payload_kind_raises() -> None:
    raw = stub_event_view(payload_kind="streaming")  # unsupported
    with pytest.raises(ValueError, match="unknown payload_kind"):
        MessageEnvelope.from_event_view(raw)


def test_envelope_bad_metadata_type_raises() -> None:
    raw = stub_event_view()
    raw["metadata"] = "not a dict"
    with pytest.raises(ValueError, match="metadata must be dict"):
        MessageEnvelope.from_event_view(raw)


def test_envelope_bad_payload_object_type_raises() -> None:
    raw = stub_event_view(
        payload_kind="object_ref",
        payload=None,
        payload_object_id="pob_00000000-0000-0000-0000-000000000001",
    )
    raw["payload_object"] = "not a dict"
    with pytest.raises(ValueError, match="payload_object must be dict"):
        MessageEnvelope.from_event_view(raw)


# ---------------------------------------------------------------------------
# (c) boundary — extreme valid values
# ---------------------------------------------------------------------------


def test_envelope_empty_payload() -> None:
    env = MessageEnvelope.from_event_view(stub_event_view(payload={}))
    assert env.payload == {}


def test_envelope_none_metadata() -> None:
    raw = stub_event_view()
    assert "metadata" not in raw
    env = MessageEnvelope.from_event_view(raw)
    assert env.metadata is None


def test_envelope_sequence_number_zero() -> None:
    env = MessageEnvelope.from_event_view(stub_event_view(sequence_number=0))
    assert env.sequence_number == 0


def test_envelope_sequence_number_large() -> None:
    env = MessageEnvelope.from_event_view(stub_event_view(sequence_number=2**63 - 1))
    assert env.sequence_number == 2**63 - 1


def test_envelope_null_event_type() -> None:
    env = MessageEnvelope.from_event_view(stub_event_view(event_type=None))
    assert env.event_type is None


# ---------------------------------------------------------------------------
# (d) attack — prefix tampering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_event_id",
    [
        "ev_abc",                     # short prefix
        "evt-abc",                    # wrong separator
        "evt_../etc/passwd",          # path traversal
        "evt_<sql>",                  # injection attempt
        "EVT_abc",                    # case mismatch
        "",                           # empty
    ],
)
def test_envelope_bad_event_id_prefix_raises(bad_event_id: str) -> None:
    with pytest.raises(ValueError, match="event_id"):
        MessageEnvelope.from_event_view(stub_event_view(event_id=bad_event_id))


@pytest.mark.parametrize(
    "bad_topic_id",
    ["topic_abc", "top-abc", "top_../etc", "TOP_abc"],
)
def test_envelope_bad_topic_id_prefix_raises(bad_topic_id: str) -> None:
    with pytest.raises(ValueError, match="topic_id"):
        MessageEnvelope.from_event_view(stub_event_view(topic_id=bad_topic_id))


def test_envelope_bad_producer_script_id_prefix_raises() -> None:
    with pytest.raises(ValueError, match="producer_script_id"):
        MessageEnvelope.from_event_view(
            stub_event_view(producer_script_id="script_abc")
        )


def test_envelope_bad_payload_object_id_prefix_raises() -> None:
    raw = stub_event_view(
        payload_kind="object_ref",
        payload=None,
        payload_object_id="payload_abc",  # wrong prefix
    )
    with pytest.raises(ValueError, match="payload_object_id"):
        MessageEnvelope.from_event_view(raw)


# ---------------------------------------------------------------------------
# PublishResult
# ---------------------------------------------------------------------------


def test_publish_result_normal_inline() -> None:
    r = PublishResult.from_response(stub_publish_response(inline_size_bytes=42))
    assert r.payload_kind == "inline"
    assert r.inline_size_bytes == 42
    assert r.payload_object_id is None


def test_publish_result_normal_object_ref() -> None:
    r = PublishResult.from_response(
        stub_publish_response(
            payload_kind="object_ref",
            inline_size_bytes=None,
            payload_object_id="pob_00000000-0000-0000-0000-000000000001",
            size_bytes=4096,
        )
    )
    assert r.payload_kind == "object_ref"
    assert r.payload_object_id.startswith("pob_")
    assert r.size_bytes == 4096
    assert r.inline_size_bytes is None


@pytest.mark.parametrize(
    "field", ["event_id", "topic_id", "sequence_number", "stored_at", "ttl_expires_at", "payload_kind"],
)
def test_publish_result_missing_field_raises(field: str) -> None:
    raw = stub_publish_response()
    raw.pop(field)
    with pytest.raises(ValueError, match="missing required field"):
        PublishResult.from_response(raw)


def test_publish_result_attack_event_id_prefix() -> None:
    with pytest.raises(ValueError, match="event_id"):
        PublishResult.from_response(stub_publish_response(event_id="forged_id"))


def test_publish_result_attack_payload_object_id_prefix() -> None:
    with pytest.raises(ValueError, match="payload_object_id"):
        PublishResult.from_response(
            stub_publish_response(
                payload_kind="object_ref",
                inline_size_bytes=None,
                payload_object_id="bogus_pob",
                size_bytes=1,
            )
        )


def test_publish_result_unknown_payload_kind() -> None:
    with pytest.raises(ValueError, match="unknown payload_kind"):
        PublishResult.from_response(stub_publish_response(payload_kind="stream"))


# ---------------------------------------------------------------------------
# ListEventsPage / PageCursor / TopicCursorState
# ---------------------------------------------------------------------------


def test_list_events_page_empty() -> None:
    page = ListEventsPage.from_response(stub_list_events_response())
    assert page.events == []
    assert page.next.has_more is False
    assert page.topic.topic_id.startswith("top_")
    assert page.clamped is False


def test_list_events_page_with_events() -> None:
    page = ListEventsPage.from_response(
        stub_list_events_response(
            events=[
                stub_event_view(sequence_number=1, event_id="evt_00000000-0000-0000-0000-000000000001"),
                stub_event_view(sequence_number=2, event_id="evt_00000000-0000-0000-0000-000000000002"),
            ],
            after="evt_00000000-0000-0000-0000-000000000002",
            after_seq=2,
            has_more=True,
        )
    )
    assert len(page.events) == 2
    assert page.events[0].sequence_number == 1
    assert page.events[1].sequence_number == 2
    assert page.next.after.startswith("evt_")
    assert page.next.has_more is True


def test_list_events_page_clamped_flag() -> None:
    page = ListEventsPage.from_response(stub_list_events_response(), clamped=True)
    assert page.clamped is True


def test_list_events_page_attack_cursor_prefix() -> None:
    body = stub_list_events_response(after="bogus_cursor", has_more=True)
    with pytest.raises(ValueError, match="next.after"):
        ListEventsPage.from_response(body)


def test_list_events_page_attack_topic_id_prefix() -> None:
    body = stub_list_events_response()
    body["topic"]["topic_id"] = "topic_xyz"
    with pytest.raises(ValueError, match="topic.topic_id"):
        ListEventsPage.from_response(body)


def test_list_events_page_missing_block_raises() -> None:
    body = stub_list_events_response()
    body.pop("topic")
    with pytest.raises(ValueError, match="missing required field"):
        ListEventsPage.from_response(body)


def test_page_cursor_and_topic_state_dataclasses_are_frozen() -> None:
    c = PageCursor(
        after="evt_1", after_seq=1, before=None, before_seq=None, has_more=False, url=None
    )
    t = TopicCursorState(
        topic_id="top_1", current_sequence=1, oldest_available_seq=1,
        oldest_available_evt_id="evt_1",
    )
    with pytest.raises(Exception):  # frozen dataclass raises FrozenInstanceError
        c.after = "evt_2"  # type: ignore[misc]
    with pytest.raises(Exception):
        t.current_sequence = 999  # type: ignore[misc]
