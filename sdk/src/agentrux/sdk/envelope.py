"""Event envelopes for AgenTrux SDK.

Represents events returned by the AgenTrux Topic data plane (Phase 2.4 + 2.5):

  GET  /topics/{id}/events           → MessageEnvelope (list)
  GET  /topics/{id}/events/{evt_id}  → MessageEnvelope (single)
  POST /topics/{id}/events           → PublishResult
  GET  /topics/{id}/events/stream    → SSE chunks (handled by sse_client)

Server SSOT:
  AgenTrux/src/agentrux/api/routers/pipe_router.py
    POST publish response : line 199-214 (_result_to_response)
    GET  event view       : line 1081-1103 (_event_view_to_dict)

All identifiers in the public-facing dict use Stripe-style prefixes:
  evt_<uuid>  event_id
  top_<uuid>  topic_id
  scr_<uuid>  producer_script_id
  pob_<uuid>  payload_object_id

Parsing enforces these prefixes — a missing or wrong prefix raises ValueError,
because server responses with wrong shape indicate a contract break that the
SDK must not silently absorb.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_iso(value: Any) -> datetime:
    """Parse server-issued ISO datetime.

    Server emits via Python `datetime.isoformat()` (pipe_router.py:204), so
    `datetime.fromisoformat` round-trips. Server is UTC-aware throughout
    (Phase 1.7c whoami fail-closed contract: tz-aware required).
    """
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected ISO datetime string, got {type(value).__name__}")
    return datetime.fromisoformat(value)


# Defense-in-depth: identifiers from a trusted server should never contain
# these characters. If they do, the response is either compromised or the
# SDK is talking to a malicious endpoint — fail fast either way.
_UNSAFE_ID_CHARS = frozenset("/<>'\"();|\\&` \t\n\r")


def _require_prefix(value: Any, prefix: str, field_name: str) -> str:
    """Validate identifier prefix; raise on mismatch or unsafe content."""
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError(
            f"{field_name} must start with {prefix!r}, got {value!r}"
        )
    suffix = value[len(prefix):]
    if not suffix:
        raise ValueError(f"{field_name} has empty suffix after {prefix!r}")
    bad = _UNSAFE_ID_CHARS.intersection(suffix)
    if bad:
        raise ValueError(
            f"{field_name} contains unsafe characters {sorted(bad)!r}: {value!r}"
        )
    return value


@dataclass(frozen=True)
class MessageEnvelope:
    """A single event as returned by GET /topics/{id}/events[/{evt_id}].

    Mirrors pipe_router._event_view_to_dict (pipe_router.py:1081-1103).
    """

    event_id: str                              # "evt_<uuid>"
    topic_id: str                              # "top_<uuid>"
    sequence_number: int
    event_type: str | None
    payload_kind: str                          # "inline" | "object_ref"
    producer_script_id: str                    # "scr_<uuid>"
    stored_at: datetime
    ttl_expires_at: datetime
    payload: Any | None = None                 # inline 時のみ
    payload_object_id: str | None = None       # "pob_<uuid>" object_ref 時のみ
    payload_object: dict[str, Any] | None = None  # expand=payload_url 時
    metadata: dict[str, Any] | None = None     # 両モード共通 (任意)
    schema: dict[str, Any] | None = None       # expand=schema 時のみ
    received_at: float = field(default_factory=time.monotonic)

    @classmethod
    def from_event_view(cls, data: dict[str, Any]) -> MessageEnvelope:
        """Parse a single event-view dict from the server."""
        try:
            event_id = _require_prefix(data["event_id"], "evt_", "event_id")
            topic_id = _require_prefix(data["topic_id"], "top_", "topic_id")
            sequence_number = int(data["sequence_number"])
            payload_kind = data["payload_kind"]
            producer_script_id = _require_prefix(
                data["producer_script_id"], "scr_", "producer_script_id"
            )
            stored_at = _parse_iso(data["stored_at"])
            ttl_expires_at = _parse_iso(data["ttl_expires_at"])
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc

        if payload_kind not in ("inline", "object_ref"):
            raise ValueError(f"unknown payload_kind: {payload_kind!r}")

        payload: Any | None = None
        payload_object_id: str | None = None
        payload_object: dict[str, Any] | None = None
        if payload_kind == "inline":
            payload = data.get("payload")
        else:
            raw_pob = data.get("payload_object_id")
            if raw_pob is not None:
                payload_object_id = _require_prefix(
                    raw_pob, "pob_", "payload_object_id"
                )
            raw_payload_object = data.get("payload_object")
            if raw_payload_object is not None and not isinstance(raw_payload_object, dict):
                raise ValueError(
                    f"payload_object must be dict|null, got "
                    f"{type(raw_payload_object).__name__}"
                )
            payload_object = raw_payload_object

        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError(f"metadata must be dict|null, got {type(metadata).__name__}")

        schema = data.get("schema")
        if schema is not None and not isinstance(schema, dict):
            raise ValueError(f"schema must be dict|null, got {type(schema).__name__}")

        return cls(
            event_id=event_id,
            topic_id=topic_id,
            sequence_number=sequence_number,
            event_type=data.get("event_type"),
            payload_kind=payload_kind,
            producer_script_id=producer_script_id,
            stored_at=stored_at,
            ttl_expires_at=ttl_expires_at,
            payload=payload,
            payload_object_id=payload_object_id,
            payload_object=payload_object,
            metadata=metadata,
            schema=schema,
        )


@dataclass(frozen=True)
class PublishResult:
    """Response of POST /topics/{id}/events.

    Mirrors pipe_router._result_to_response (pipe_router.py:199-214).
    payload_kind dictates which size field is populated:
      - "inline"     → inline_size_bytes
      - "object_ref" → payload_object_id + size_bytes
    """

    event_id: str
    topic_id: str
    sequence_number: int
    stored_at: datetime
    ttl_expires_at: datetime
    payload_kind: str
    inline_size_bytes: int | None = None
    payload_object_id: str | None = None
    size_bytes: int | None = None

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> PublishResult:
        try:
            event_id = _require_prefix(data["event_id"], "evt_", "event_id")
            topic_id = _require_prefix(data["topic_id"], "top_", "topic_id")
            sequence_number = int(data["sequence_number"])
            stored_at = _parse_iso(data["stored_at"])
            ttl_expires_at = _parse_iso(data["ttl_expires_at"])
            payload_kind = data["payload_kind"]
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc

        if payload_kind not in ("inline", "object_ref"):
            raise ValueError(f"unknown payload_kind: {payload_kind!r}")

        inline_size_bytes: int | None = None
        payload_object_id: str | None = None
        size_bytes: int | None = None
        if payload_kind == "inline":
            if "inline_size_bytes" in data and data["inline_size_bytes"] is not None:
                inline_size_bytes = int(data["inline_size_bytes"])
        else:
            raw_pob = data.get("payload_object_id")
            if raw_pob is not None:
                payload_object_id = _require_prefix(
                    raw_pob, "pob_", "payload_object_id"
                )
            if "size_bytes" in data and data["size_bytes"] is not None:
                size_bytes = int(data["size_bytes"])

        return cls(
            event_id=event_id,
            topic_id=topic_id,
            sequence_number=sequence_number,
            stored_at=stored_at,
            ttl_expires_at=ttl_expires_at,
            payload_kind=payload_kind,
            inline_size_bytes=inline_size_bytes,
            payload_object_id=payload_object_id,
            size_bytes=size_bytes,
        )


@dataclass(frozen=True)
class PageCursor:
    """`next` block of GET /topics/{id}/events response.

    Mirrors pipe_router.py:1276-1283.
    Either after/after_seq or before/before_seq is populated depending on
    the request's order; the other pair is None.
    """

    after: str | None                 # "evt_<uuid>" or None
    after_seq: int | None
    before: str | None                # "evt_<uuid>" or None
    before_seq: int | None
    has_more: bool
    url: str | None                   # next page URL (already query-string built)


@dataclass(frozen=True)
class TopicCursorState:
    """`topic` block of GET /topics/{id}/events response.

    Mirrors pipe_router.py:1284-1291. Tells the client the current head
    sequence and the retention boundary, so it can detect "fell off the
    retention edge" without a probe.
    """

    topic_id: str                     # "top_<uuid>"
    current_sequence: int
    oldest_available_seq: int | None
    oldest_available_evt_id: str | None  # "evt_<uuid>" or None


@dataclass(frozen=True)
class ListEventsPage:
    """Full response of GET /topics/{id}/events.

    Combines event view list + paging cursor + topic head state.
    `clamped` reflects the X-AgenTrux-Pagination: clamped response header
    (pipe_router.py:1293-1295) — true when the server capped `limit`.
    """

    events: list[MessageEnvelope]
    next: PageCursor
    topic: TopicCursorState
    clamped: bool = False

    @classmethod
    def from_response(
        cls, body: dict[str, Any], *, clamped: bool = False
    ) -> ListEventsPage:
        try:
            events_raw = body["events"]
            next_raw = body["next"]
            topic_raw = body["topic"]
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc

        if not isinstance(events_raw, list):
            raise ValueError(f"events must be list, got {type(events_raw).__name__}")
        events = [MessageEnvelope.from_event_view(e) for e in events_raw]

        # next block (cursor)
        next_after = next_raw.get("after")
        if next_after is not None:
            next_after = _require_prefix(next_after, "evt_", "next.after")
        next_before = next_raw.get("before")
        if next_before is not None:
            next_before = _require_prefix(next_before, "evt_", "next.before")
        page = PageCursor(
            after=next_after,
            after_seq=(int(next_raw["after_seq"]) if next_raw.get("after_seq") is not None else None),
            before=next_before,
            before_seq=(int(next_raw["before_seq"]) if next_raw.get("before_seq") is not None else None),
            has_more=bool(next_raw.get("has_more", False)),
            url=next_raw.get("url"),
        )

        # topic block
        topic_id = _require_prefix(topic_raw["topic_id"], "top_", "topic.topic_id")
        oldest_evt = topic_raw.get("oldest_available_evt_id")
        if oldest_evt is not None:
            oldest_evt = _require_prefix(oldest_evt, "evt_", "topic.oldest_available_evt_id")
        topic = TopicCursorState(
            topic_id=topic_id,
            current_sequence=int(topic_raw["current_sequence"]),
            oldest_available_seq=(
                int(topic_raw["oldest_available_seq"])
                if topic_raw.get("oldest_available_seq") is not None
                else None
            ),
            oldest_available_evt_id=oldest_evt,
        )

        return cls(events=events, next=page, topic=topic, clamped=clamped)
