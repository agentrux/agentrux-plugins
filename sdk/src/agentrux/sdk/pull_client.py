"""Pull mode read — GET /events polling.

SSOT: docs/04_design/sdk/sdk_design.md §5-1, docs/04_design/messaging/read_flow.md
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING

from agentrux.sdk.errors import (
    AgenTruxError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)
from agentrux.sdk.models import Event

if TYPE_CHECKING:
    from agentrux.sdk.facade import AgentRuxClient


def _parse_event(raw: dict) -> Event:
    """server response の event item を SDK Event に変換."""
    return Event(
        event_id=raw["event_id"],
        topic_id=raw["topic_id"],
        event_type=raw.get("event_type", "user.event"),
        sequence_number=int(raw["sequence_number"]),
        occurred_at=datetime.fromisoformat(raw["occurred_at"].replace("Z", "+00:00"))
        if isinstance(raw["occurred_at"], str)
        else raw["occurred_at"],
        payload=raw.get("payload"),
        payload_object_ref=raw.get("payload_object_ref"),
        metadata=raw.get("metadata"),
    )


def _map_read_error(status: int, body_text: str) -> Exception:
    if status == 403:
        return PermissionDeniedError(f"scope_mismatch: {body_text}")
    if status == 404:
        return ResourceNotFoundError(f"topic not found: {body_text}")
    if status == 422:
        return ValidationError(f"invalid read query: {body_text}")
    return AgenTruxError(f"read failed with {status}: {body_text}")


async def read_pull(
    client: AgentRuxClient,
    *,
    topic_id: str,
    after: str | None = None,
    limit: int = 100,
    poll_interval_seconds: float = 1.0,
    stop_when_empty: bool = False,
) -> AsyncIterator[Event]:
    """GET /topics/{top_id}/events?after=&limit= を loop で polling.

    Args:
      after: cursor (evt_<uuid>)、 None なら server 側 default (最古から)
      limit: 1..100
      poll_interval_seconds: has_more=False で sleep してから再試行
      stop_when_empty: True なら最初に has_more=False を見たら break (test 用)
    """
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")
    if limit < 1 or limit > 100:
        raise ValidationError(f"limit must be 1..100 (got {limit})")

    cursor = after
    while True:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["after"] = cursor
        r = await client._request("GET", f"/topics/{topic_id}/events", params=params)
        if r.status_code != 200:
            raise _map_read_error(r.status_code, r.text)
        body = r.json()
        items = body.get("items", [])
        for raw in items:
            evt = _parse_event(raw)
            yield evt
            cursor = evt.event_id

        has_more = bool(body.get("has_more"))
        if not has_more:
            if stop_when_empty:
                return
            await asyncio.sleep(poll_interval_seconds)
