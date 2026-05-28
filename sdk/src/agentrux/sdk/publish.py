"""Publish API — inline (<= 256 KiB) / object_ref 自動切替.

SSOT: docs/04_design/sdk/sdk_design.md §4, docs/04_design/messaging/large_payload.md §10
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx

# Test-only override point: monkeypatch this attribute to substitute the direct
# PUT client for presigned URL uploads without touching the global httpx module.
_DirectPUTClient = httpx.AsyncClient

from agentrux.sdk.errors import (
    AgenTruxError,
    ConflictError,
    IdempotencyConflictError,
    PayloadTooLargeError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ValidationError,
)
from agentrux.sdk.models import PublishResult

if TYPE_CHECKING:
    from agentrux.sdk.facade import AgentRuxClient

INLINE_MAX_BYTES = 256 * 1024  # spec large_payload.md §10


def _serialize(payload: Any) -> bytes:
    """payload を bytes に正規化."""
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, (dict, list)):
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if hasattr(payload, "model_dump_json"):  # Pydantic v2 BaseModel
        return payload.model_dump_json().encode("utf-8")
    raise ValidationError(f"unsupported payload type: {type(payload).__name__}")


def _validate_topic_id(topic_id: str) -> None:
    if not topic_id.startswith("top_"):
        raise ValidationError(f"topic_id must start with 'top_': {topic_id!r}")


def _map_publish_error(r: httpx.Response) -> Exception:
    """HTTP response → SDK 例外 mapping (sdk_design.md §4-2)."""
    try:
        body = r.json()
        err = body.get("error", {})
        code = err.get("code") if isinstance(err, dict) else None
        message = err.get("message") if isinstance(err, dict) else str(body)
    except Exception:
        code = None
        message = r.text

    if r.status_code == 403:
        return PermissionDeniedError(f"scope_mismatch: {message}")
    if r.status_code == 404:
        return ResourceNotFoundError(f"topic not found or not accessible: {message}")
    if r.status_code == 409:
        if code == "IDEMPOTENCY_CONFLICT" or "idempotency" in (message or "").lower():
            return IdempotencyConflictError(f"idempotency body mismatch: {message}")
        return ConflictError(f"conflict: {message}")
    if r.status_code == 413:
        return PayloadTooLargeError(f"payload too large: {message}")
    if r.status_code == 422:
        return ValidationError(f"invalid request: {message}")
    return AgenTruxError(f"publish failed with {r.status_code}: {message}")


async def publish(
    client: AgentRuxClient,
    *,
    topic_id: str,
    payload: Any,
    event_type: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> PublishResult:
    """size に応じて inline / object_ref を自動選択して publish.

    Args:
      client: AgentRuxClient (auth + http を提供)
      topic_id: "top_<uuid>" prefix 付き
      payload: bytes / dict / list / Pydantic BaseModel
      event_type: optional (default None = サーバー側 "user.event")
      idempotency_key: None なら client が uuid4 生成
      metadata: 任意の小辞書 (server 側で event.metadata_json に格納)

    Returns: PublishResult(event_id, sequence_number, idempotent_replayed)
    """
    _validate_topic_id(topic_id)
    raw = _serialize(payload)
    idk = idempotency_key or f"idk_{uuid.uuid4()}"

    if len(raw) <= INLINE_MAX_BYTES:
        return await _publish_inline(
            client,
            topic_id=topic_id,
            raw=raw,
            event_type=event_type,
            idempotency_key=idk,
            metadata=metadata,
        )
    return await _publish_object_ref(
        client,
        topic_id=topic_id,
        raw=raw,
        event_type=event_type,
        idempotency_key=idk,
        metadata=metadata,
    )


async def _publish_inline(
    client: AgentRuxClient,
    *,
    topic_id: str,
    raw: bytes,
    event_type: str | None,
    idempotency_key: str,
    metadata: dict | None,
) -> PublishResult:
    """POST /topics/{top_id}/events with inline payload."""
    body: dict[str, Any] = {"payload": json.loads(raw.decode("utf-8"))} if _looks_json(raw) else {"payload_b64": raw.hex()}
    if event_type is not None:
        body["event_type"] = event_type
    if metadata is not None:
        body["metadata"] = metadata

    r = await client._request(
        "POST",
        f"/topics/{topic_id}/events",
        json=body,
        headers={"Idempotency-Key": idempotency_key},
    )
    if r.status_code not in (200, 201):
        raise _map_publish_error(r)
    rb = r.json()
    return PublishResult(
        event_id=rb["event_id"],
        sequence_number=int(rb["sequence_number"]),
        idempotent_replayed=r.headers.get("Idempotent-Replayed") == "true",
    )


async def _publish_object_ref(
    client: AgentRuxClient,
    *,
    topic_id: str,
    raw: bytes,
    event_type: str | None,
    idempotency_key: str,
    metadata: dict | None,
) -> PublishResult:
    """presigned PUT → commit の 3 step flow."""
    sha256 = hashlib.sha256(raw).hexdigest()
    # Step 1: POST /topics/{top_id}/payloads
    r1 = await client._request(
        "POST",
        f"/topics/{topic_id}/payloads",
        json={"size_bytes": len(raw), "checksum_sha256": sha256, "content_type": "application/octet-stream"},
    )
    if r1.status_code not in (200, 201):
        raise _map_publish_error(r1)
    pres = r1.json()
    pob_id = pres["payload_object_id"]
    put_url = pres["put_url"]

    # Step 2: PUT (presigned, AgenTrux server を経由しない直 upload)
    async with _DirectPUTClient(timeout=60.0) as direct:
        rp = await direct.put(put_url, content=raw, headers={"Content-Type": "application/octet-stream"})
    if rp.status_code not in (200, 201, 204):
        raise AgenTruxError(f"presigned PUT failed: {rp.status_code}")

    # Step 3: commit via publish event with object_ref
    body: dict[str, Any] = {"object_ref": pob_id}
    if event_type is not None:
        body["event_type"] = event_type
    if metadata is not None:
        body["metadata"] = metadata

    r3 = await client._request(
        "POST",
        f"/topics/{topic_id}/events",
        json=body,
        headers={"Idempotency-Key": idempotency_key},
    )
    if r3.status_code not in (200, 201):
        raise _map_publish_error(r3)
    rb = r3.json()
    return PublishResult(
        event_id=rb["event_id"],
        sequence_number=int(rb["sequence_number"]),
        idempotent_replayed=r3.headers.get("Idempotent-Replayed") == "true",
    )


def _looks_json(raw: bytes) -> bool:
    """raw が valid JSON object/array なら True."""
    if not raw:
        return False
    first = raw.lstrip()[:1]
    return first in (b"{", b"[")
