"""AgenTrux API client for Dify plugin (OAuth 2.1).

Two auth paths funnel into a single bearer token:
  - OAuth flow:           Dify supplies credentials["access_token"]; we use it
                          directly. Dify auto-refreshes via _oauth_refresh.
  - client_credentials:   credentials["client_id"] / ["client_secret"] are
                          present; we exchange them for a JWT and cache the
                          result in-memory (60 s expiry buffer).

All HTTP failures bubble up as httpx exceptions so tools can surface a
human-readable message.
"""
from __future__ import annotations

import base64
import json
import time

import httpx

# Per-process cache for client_credentials JWTs.
# key = "<base_url>::<client_id>"  ->  (access_token, expires_at_epoch)
_cc_token_cache: dict[str, tuple[str, float]] = {}


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

def _is_url_allowed(base_url: str) -> bool:
    if base_url.startswith("https://"):
        return True
    if base_url.startswith("http://localhost") or base_url.startswith(
        "http://127.0.0.1"
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------

def resolve_access_token(creds: dict) -> tuple[str, str]:
    """Resolve (base_url, access_token) from runtime.credentials.

    Order:
      1. If access_token is present (OAuth path), return it.
      2. Else fall back to client_credentials grant.
    """
    base_url = creds.get("base_url") or "https://api.agentrux.com"
    if not _is_url_allowed(base_url):
        raise ValueError(f"base_url must use HTTPS (got {base_url!r})")

    access_token = creds.get("access_token") or ""
    if access_token:
        return base_url, access_token

    client_id = creds.get("client_id") or ""
    client_secret = creds.get("client_secret") or ""
    if not client_id or not client_secret:
        raise ValueError(
            "No credentials available — connect via OAuth or paste Script client_id/client_secret"
        )
    return base_url, _client_credentials_token(base_url, client_id, client_secret)


def _client_credentials_token(base_url: str, client_id: str, client_secret: str) -> str:
    cache_key = f"{base_url}::{client_id}"
    cached = _cc_token_cache.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    # Resolve the token endpoint via RFC 8414 discovery so this code
    # tracks any future endpoint moves at the AgenTrux backend without
    # a plugin re-release. Cached per base_url inside agentrux_tools.
    from .agentrux_tools import _discover_metadata
    token_endpoint = _discover_metadata(base_url)["token_endpoint"]

    resp = httpx.post(
        token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    expires_at = time.time() + int(body.get("expires_in", 3600))
    token = body["access_token"]
    _cc_token_cache[cache_key] = (token, expires_at)
    return token


def auth_headers(creds: dict) -> tuple[str, dict[str, str]]:
    base_url, token = resolve_access_token(creds)
    return base_url, {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# JWT scope decode -> dynamic-select options
# ---------------------------------------------------------------------------

def _decode_jwt_scope(token: str) -> list[str]:
    parts = token.split(".")
    if len(parts) < 2:
        return []
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return []
    scope = claims.get("scope", [])
    if isinstance(scope, str):
        return scope.split()
    return list(scope) if isinstance(scope, list) else []


def build_topic_options(creds: dict, allowed_actions: set[str]) -> list[dict]:
    """Build dynamic-select options from JWT scope claim.

    Scope entries shaped like 'topic:<topic_id>:<action>' are extracted.
    """
    try:
        base_url, token = resolve_access_token(creds)
    except Exception:
        return []

    seen: set[str] = set()
    options: list[dict] = []
    for entry in _decode_jwt_scope(token):
        if not entry.startswith("topic:"):
            continue
        parts = entry.split(":")
        if len(parts) < 3:
            continue
        topic_id, action = parts[1], parts[2]
        if action not in allowed_actions or topic_id in seen:
            continue
        seen.add(topic_id)
        options.append({"label": f"{topic_id} ({action})", "value": topic_id})
    return options


# ---------------------------------------------------------------------------
# PubSub operations
# ---------------------------------------------------------------------------

def publish_event(
    creds: dict,
    topic_id: str,
    event_type: str,
    payload: dict,
    correlation_id: str | None = None,
    reply_topic: str | None = None,
) -> dict:
    base_url, headers = auth_headers(creds)
    body: dict = {"type": event_type, "payload": payload}
    if correlation_id:
        body["correlation_id"] = correlation_id
    if reply_topic:
        body["reply_topic"] = reply_topic
    resp = httpx.post(
        f"{base_url}/topics/{topic_id}/events",
        json=body,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def read_events(
    creds: dict,
    topic_id: str,
    after_sequence_no: int = 0,
    limit: int = 20,
    event_type: str | None = None,
) -> list[dict]:
    base_url, headers = auth_headers(creds)
    params: dict = {"after_sequence_no": str(after_sequence_no), "limit": str(limit)}
    if event_type:
        params["type"] = event_type
    resp = httpx.get(
        f"{base_url}/topics/{topic_id}/events",
        params=params,
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("items", [])


def create_payload(
    creds: dict,
    topic_id: str,
    content_type: str,
    filename: str,
    size: int,
) -> dict:
    base_url, headers = auth_headers(creds)
    resp = httpx.post(
        f"{base_url}/topics/{topic_id}/payloads",
        json={"content_type": content_type, "filename": filename, "size": size},
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def upload_to_presigned(upload_url: str, data: bytes, content_type: str) -> None:
    resp = httpx.put(
        upload_url,
        content=data,
        headers={"Content-Type": content_type},
        timeout=30,
    )
    resp.raise_for_status()
