"""pytest fixtures shared across the SDK test-suite.

Design:
- Tests never touch the network. All HTTP is intercepted via
  `httpx.MockTransport`, which lets each test declaratively control
  the (request → response) mapping.
- `make_api_client` builds an `AgenTruxAPIClient` wired to the mock
  transport, optionally with a TokenManager for /topics/* tests.
- Helpers `stub_publish_response`, `stub_list_events_response`,
  `stub_oauth_token_response` mint server-shaped response dicts so
  individual tests don't repeat the schema.
- `_jwt(...)` synthesises a minimal unsigned JWT with the requested
  `exp` claim — the client only decodes for refresh-timing, never
  verifies, so the signature can be junk.
"""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agentrux.sdk.client import AgenTruxAPIClient, TokenManager


# Make the asyncio loop the default so individual tests don't need
# `@pytest.mark.asyncio` decorators everywhere.
pytestmark_asyncio_auto = True


@pytest.fixture
def base_url() -> str:
    return "https://api.agentrux.test"


def _jwt(exp_unix: int) -> str:
    """Return an unsigned JWT with a single `exp` claim."""
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode()
    payload_dict = {"exp": exp_unix}
    payload = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).rstrip(b"=").decode()
    # signature is a junk placeholder; the SDK never verifies.
    return f"{header}.{payload}.sig"


@pytest.fixture
def make_jwt() -> Callable[[int], str]:
    return _jwt


@pytest.fixture
def fresh_access_token() -> str:
    return _jwt(int(time.time()) + 3600)


@pytest.fixture
def expiring_access_token() -> str:
    return _jwt(int(time.time()) + 30)  # < REFRESH_THRESHOLD_SECONDS (60)


@pytest.fixture
def expired_access_token() -> str:
    return _jwt(int(time.time()) - 60)


# ---------------------------------------------------------------------------
# Response shape helpers (mirror server's pipe_router / oauth_router)
# ---------------------------------------------------------------------------


def stub_publish_response(
    *,
    event_id: str = "evt_00000000-0000-0000-0000-000000000001",
    topic_id: str = "top_00000000-0000-0000-0000-000000000001",
    sequence_number: int = 1,
    payload_kind: str = "inline",
    inline_size_bytes: int | None = 12,
    payload_object_id: str | None = None,
    size_bytes: int | None = None,
    stored_at: str = "2026-05-24T10:00:00+00:00",
    ttl_expires_at: str = "2026-05-25T10:00:00+00:00",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": event_id,
        "topic_id": topic_id,
        "sequence_number": sequence_number,
        "payload_kind": payload_kind,
        "stored_at": stored_at,
        "ttl_expires_at": ttl_expires_at,
    }
    if payload_kind == "inline":
        out["inline_size_bytes"] = inline_size_bytes
    else:
        out["payload_object_id"] = payload_object_id
        out["size_bytes"] = size_bytes
    return out


def stub_event_view(
    *,
    event_id: str = "evt_00000000-0000-0000-0000-000000000001",
    topic_id: str = "top_00000000-0000-0000-0000-000000000001",
    sequence_number: int = 1,
    event_type: str | None = "hello.world",
    payload_kind: str = "inline",
    producer_script_id: str = "scr_00000000-0000-0000-0000-000000000001",
    payload: Any = None,
    payload_object_id: str | None = None,
    metadata: dict | None = None,
    stored_at: str = "2026-05-24T10:00:00+00:00",
    ttl_expires_at: str = "2026-05-25T10:00:00+00:00",
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": event_id,
        "topic_id": topic_id,
        "sequence_number": sequence_number,
        "event_type": event_type,
        "payload_kind": payload_kind,
        "producer_script_id": producer_script_id,
        "stored_at": stored_at,
        "ttl_expires_at": ttl_expires_at,
    }
    if payload_kind == "inline":
        out["payload"] = payload
    else:
        out["payload_object_id"] = payload_object_id
    if metadata is not None:
        out["metadata"] = metadata
    return out


def stub_list_events_response(
    *,
    events: list[dict] | None = None,
    after: str | None = None,
    after_seq: int | None = None,
    has_more: bool = False,
    topic_id: str = "top_00000000-0000-0000-0000-000000000001",
    current_sequence: int = 1,
    oldest_available_seq: int | None = 1,
    oldest_available_evt_id: str | None = "evt_00000000-0000-0000-0000-000000000001",
) -> dict[str, Any]:
    return {
        "events": events or [],
        "next": {
            "after": after,
            "after_seq": after_seq,
            "before": None,
            "before_seq": None,
            "has_more": has_more,
            "url": (
                f"/topics/{topic_id}/events?after={after}&limit=100"
                if has_more and after
                else None
            ),
        },
        "topic": {
            "topic_id": topic_id,
            "current_sequence": current_sequence,
            "oldest_available_seq": oldest_available_seq,
            "oldest_available_evt_id": oldest_available_evt_id,
        },
    }


def stub_oauth_token_response(
    *,
    access_token: str = "aat_eyJtest",
    refresh_token: str | None = None,
    expires_in: int = 3600,
    scope: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    if refresh_token is not None:
        out["refresh_token"] = refresh_token
    if scope is not None:
        out["scope"] = scope
    return out


def stub_pipe_error(
    *,
    error: str = "INVALID",
    message: str = "test error",
    details: dict | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """FastAPI-style error body (wrapped in `detail`)."""
    inner: dict[str, Any] = {"error": error, "message": message}
    if details is not None:
        inner["details"] = details
    if next_action is not None:
        inner["next_action"] = next_action
    return {"detail": inner}


def stub_oauth_error(
    *, error: str = "invalid_grant", error_description: str = "test"
) -> dict[str, Any]:
    return {"error": error, "error_description": error_description}


# Re-export helpers so tests can `from conftest import stub_publish_response`.
__all__ = [
    "stub_publish_response",
    "stub_event_view",
    "stub_list_events_response",
    "stub_oauth_token_response",
    "stub_pipe_error",
    "stub_oauth_error",
]


# ---------------------------------------------------------------------------
# HTTP transport / client fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_api_client(base_url: str, fresh_access_token: str):
    """Return a factory that builds an AgenTruxAPIClient on a mock transport.

    Usage:

        async def test_publish_ok(make_api_client):
            handlers = {("POST", "/topics/top_abc/events"): lambda req: httpx.Response(200, json=stub_publish_response())}
            async with await make_api_client(handlers) as api:
                ...
    """

    def _factory(
        handlers: dict[tuple[str, str], Callable[[httpx.Request], httpx.Response]],
        *,
        with_token: bool = True,
        access_token: str | None = None,
    ):
        def _route(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            handler = handlers.get(key)
            if handler is None:
                return httpx.Response(
                    404, json={"detail": {"error": "NOT_FOUND", "message": f"no stub for {key}"}}
                )
            return handler(request)

        transport = httpx.MockTransport(_route)
        http = httpx.AsyncClient(transport=transport, base_url=base_url)
        tm: TokenManager | None = None
        if with_token:
            token = access_token or fresh_access_token
            tm = TokenManager(access_token=token)
        return AgenTruxAPIClient(base_url=base_url, token_manager=tm, http=http)

    return _factory
