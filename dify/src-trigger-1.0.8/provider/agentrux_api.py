"""AgenTrux API client for the Trigger plugin.

HTTP via stdlib urllib.request (gevent-compatible; httpx hangs under
dify_plugin's monkey.patch_all). Includes AC-based activation with disk
cache so re-save is idempotent.

Phase 1.9+ (2026-05-15) onwards:
  - POST /auth/redeem-activation-code body {code} -> {client_id, client_secret, script_id, issued_at}
  - POST /oauth/token (form-encoded, grant_type=client_credentials) -> {access_token, expires_in, ...}
The legacy /auth/activate, /auth/token, /auth/refresh endpoints are gone.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import pathlib
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

ACTIVATED_CACHE: dict[str, tuple[str, str]] = {}
_token_cache: dict[str, tuple[str, float]] = {}
_DISK_CACHE_FILE = pathlib.Path(".agentrux_activated.json")


# ----------------------------------------------------------------------------
# Dify SDK endpoint helpers
# ----------------------------------------------------------------------------
# Dify allocates webhook endpoints as `http://localhost/triggers/plugin/<sub_id>`.
# Two facts the SDK does not expose nicely:
#   1) subscription_id is only retrievable by parsing this path.
#   2) `localhost` is the plugin daemon container, not the Dify api — outbound
#      loopback POST must be rewritten to DIFY_INNER_API_URL (= http://api:5001).
# Centralising both so SDK shape changes touch one place, not 3.

DIFY_LOOPBACK_HOST = "http://localhost"
DIFY_INNER_API_ENV = "DIFY_INNER_API_URL"
_SUBSCRIPTION_PATH_PREFIX = "/triggers/plugin/"


def parse_subscription_id(endpoint: str) -> str:
    """Return the subscription_id encoded in the Dify-allocated endpoint URL.

    Returns "" if the endpoint shape is unrecognised (caller can fall back
    to "ignore" semantics — cursor / per-sub state is best-effort).
    """
    if _SUBSCRIPTION_PATH_PREFIX not in endpoint:
        return ""
    return endpoint.rsplit(_SUBSCRIPTION_PATH_PREFIX, 1)[-1]


def rewrite_endpoint_for_inner_api(endpoint: str) -> str:
    """Rewrite Dify's `http://localhost/...` endpoint to the routable inner API URL.

    Inside the plugin daemon container `localhost` does not reach the Dify api
    service — DIFY_INNER_API_URL (e.g. `http://api:5001`) is the routable name.
    Returns the endpoint unchanged when:
      - DIFY_INNER_API_URL is unset (local dev / direct webhook delivery)
      - endpoint already targets an external host (production webhook URL)
    """
    inner_api = os.environ.get(DIFY_INNER_API_ENV, "").rstrip("/")
    if inner_api and endpoint.startswith(DIFY_LOOPBACK_HOST):
        return inner_api + endpoint[len(DIFY_LOOPBACK_HOST):]
    return endpoint


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def is_ttl_expired_cursor(err: BaseException) -> bool:
    """True when a read_events 404 means the `?after=` cursor aged out of retention.

    `pipe_router._ttl_expired_cursor_response` answers a TTL-expired cursor with
    404 and (FastAPI-wrapped under `detail`):
        {"detail": {"error": "NOT_FOUND",
                    "details": {"reason": "ttl_expired", "oldest_available_evt_id": ...},
                    "next_action": "cursor_advance"}}
    The pinned cursor can never become valid again, so the caller must re-anchor
    (chat: skip-to-latest). Mirrors openclaw http-client.ts isTtlExpiredCursor.
    """
    if not isinstance(err, HttpError) or err.status != 404:
        return False
    try:
        body = json.loads(err.body) if err.body else {}
    except (ValueError, TypeError):
        return False
    if not isinstance(body, dict):
        return False
    detail = body.get("detail", body)
    if not isinstance(detail, dict):
        return False
    details = detail.get("details")
    reason = details.get("reason") if isinstance(details, dict) else None
    if reason == "ttl_expired":
        return True
    # `next_action` is only a fallback for a server that omitted `reason`
    # entirely (forward-compat). Never treat a *different* reason as TTL just
    # because next_action says advance — that would misclassify other 404s.
    return not reason and detail.get("next_action") == "cursor_advance"


def _ensure_top_prefix(topic_id: str) -> str:
    """Ensure the `top_` prefix for server-side path params.

    `pipe_router` enforces `top_<uuid>` on all data-plane endpoints. Callers
    may pass a bare UUID for backwards compatibility; we normalize.
    """
    return topic_id if topic_id.startswith("top_") else f"top_{topic_id}"


def _http_json(method: str, url: str, body: dict | None = None,
               headers: dict | None = None, timeout: float = 15) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise HttpError(e.code, raw) from None


def _ac_fingerprint(ac: str) -> str:
    return hashlib.sha256(ac.encode("utf-8")).hexdigest()


def _load_disk_cache() -> dict[str, dict]:
    try:
        if not _DISK_CACHE_FILE.is_file():
            return {}
        return json.loads(_DISK_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("disk cache read failed: %s", e)
        return {}


def _save_disk_cache(cache: dict[str, dict]) -> None:
    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(_DISK_CACHE_FILE.parent) or ".",
            prefix=".agentrux_activated.", suffix=".tmp", delete=False,
        )
        try:
            json.dump(cache, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, _DISK_CACHE_FILE)
    except Exception as e:
        logger.warning("disk cache write failed: %s", e)


def activate(base_url: str, activation_code: str) -> tuple[str, str]:
    """AC を消費 (server-single-use)。 (client_id, client_secret) を返す。"""
    d = _http_json("POST", f"{base_url}/auth/redeem-activation-code",
                   body={"code": activation_code}, timeout=10)
    return d["client_id"], d["client_secret"]


def resolve_credentials_from_cache(base_url: str) -> tuple[str, str] | None:
    """Return (client_id, client_secret) from disk cache for the given base_url.

    No AC required — iterates the disk cache and returns the first entry whose
    base_url matches. This lets long-lived components (SSE worker, event
    handler) work off the cached credential without re-storing the AC.

    Returns None if no matching entry exists (caller should refuse to act
    rather than silently activate with a possibly-stale or wrong AC).

    Process-local ACTIVATED_CACHE is consulted first for a 0 IO fast path.
    """
    cached = ACTIVATED_CACHE.get(base_url)
    if cached:
        return cached
    for entry in _load_disk_cache().values():
        if entry.get("base_url") == base_url and entry.get("client_id"):
            cid, secret = entry["client_id"], entry["client_secret"]
            ACTIVATED_CACHE[base_url] = (cid, secret)
            return cid, secret
    return None


def validate_activation(base_url: str, activation_code: str) -> tuple[str, str]:
    """AC を消費し (client_id, client_secret) を返す。 disk cache で冪等。

    Legacy entry (`script_id` キーだけで `client_id` を持たない) はサーバが
    /auth/token endpoint を廃止したため fail-fast し、 再 activate に進む。
    """
    fp = _ac_fingerprint(activation_code)
    cache = _load_disk_cache()
    entry = cache.get(fp)
    if entry and entry.get("base_url") == base_url and entry.get("client_id"):
        cid, secret = entry["client_id"], entry["client_secret"]
        ACTIVATED_CACHE[base_url] = (cid, secret)
        return cid, secret

    cid, secret = activate(base_url, activation_code)
    cache[fp] = {
        "base_url": base_url,
        "client_id": cid,
        "client_secret": secret,
        "activated_at": int(time.time()),
    }
    _save_disk_cache(cache)
    ACTIVATED_CACHE[base_url] = (cid, secret)
    return cid, secret


def get_token(base_url: str, client_id: str, client_secret: str) -> str:
    """POST /oauth/token grant_type=client_credentials (form-encoded)。 60 秒バッファ付き cache。"""
    key = f"{base_url}::{client_id}"
    cached = _token_cache.get(key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]

    form_body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/oauth/token",
        data=form_body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise HttpError(e.code, raw) from None

    expires_in = int(data.get("expires_in", 600))
    _token_cache[key] = (data["access_token"], time.time() + expires_in)
    return data["access_token"]


def auth_headers(base_url: str, client_id: str, client_secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {get_token(base_url, client_id, client_secret)}"}


def fetch_granted_topics(base_url: str, client_id: str, client_secret: str) -> list[dict]:
    token = get_token(base_url, client_id, client_secret)
    parts = token.split(".")
    if len(parts) < 2:
        return []
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return []

    topics, seen = [], set()
    for s in claims.get("scope", []):
        if not s.startswith("topic:"):
            continue
        p = s.split(":")
        if len(p) >= 3:
            k = f"{p[1]}:{p[2]}"
            if k not in seen:
                seen.add(k)
                topics.append({"topic_id": p[1], "action": p[2]})
    return topics


def read_events(base_url: str, client_id: str, client_secret: str, topic_id: str,
                after_event_id: str | None = None, limit: int = 20,
                event_type: str | None = None, order: str = "asc") -> list[dict]:
    """GET /topics/{top_id}/events (Phase 2.5a SSOT)。

    Cursor は evt_id 文字列 (旧 ?after_sequence_no は廃止)。
    Response shape: {events: [...], next: {...}}。 旧 items は廃止。
    each event: {event_id, sequence_number, event_type, payload, payload_object_id?, ...}
    """
    from urllib.parse import quote, urlencode
    # Echo Policy V1: exclude_self=true で caller (= 同じ script credential で
    # publish した) 自身のイベントを除外。 round-trip workflow が自分の reply
    # に再 trigger される無限ループを防ぐ。
    qs: dict = {"limit": str(limit), "order": order, "exclude_self": "true"}
    if after_event_id:
        qs["after"] = after_event_id
    if event_type:
        qs["type"] = event_type
    params = "?" + urlencode(qs, quote_via=quote)
    top_id = _ensure_top_prefix(topic_id)
    data = _http_json("GET", f"{base_url}/topics/{top_id}/events{params}",
                      headers=auth_headers(base_url, client_id, client_secret), timeout=15)
    return data.get("events", [])


def get_payload_download_url(base_url: str, client_id: str, client_secret: str,
                             topic_id: str, object_id: str) -> str:
    """GET /topics/{top_id}/payloads/{pob_id} (Phase 2.4c SSOT)。

    Response field: `presigned_get_url` (旧 `download_url` は廃止)。
    """
    top_id = _ensure_top_prefix(topic_id)
    data = _http_json("GET", f"{base_url}/topics/{top_id}/payloads/{object_id}",
                      headers=auth_headers(base_url, client_id, client_secret), timeout=10)
    return data.get("presigned_get_url", "")
