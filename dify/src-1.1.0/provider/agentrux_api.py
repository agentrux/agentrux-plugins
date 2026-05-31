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
import hashlib
import json
import logging
import os
import pathlib
import tempfile
import time

import httpx

logger = logging.getLogger(__name__)

# Per-process cache for client_credentials JWTs.
# key = "<base_url>::<client_id>"  ->  (access_token, expires_at_epoch)
_cc_token_cache: dict[str, tuple[str, float]] = {}

# Activation Code -> Script credential cache (mirrors the trigger plugin so the
# same act_ -> crd_/aks_ -> client_credentials flow works in tools).
# key = "<base_url>" -> (client_id, client_secret); disk file is 0600 and keyed
# by sha256(activation_code) so re-saving the same code is idempotent.
ACTIVATED_CACHE: dict[str, tuple[str, str]] = {}
# Absolute path next to this module, NOT the CWD: the Dify plugin daemon runs
# several subprocesses with different working directories (e.g. CWD=/app vs the
# install dir), so a relative path makes one subprocess write the cache while
# another can't find it — the dynamic-select dropdown then re-redeems a
# single-use code, fails, and silently returns no options.
_DISK_CACHE_FILE = pathlib.Path(__file__).resolve().parent / ".agentrux_activated.json"


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

def _ac_fingerprint(activation_code: str) -> str:
    return hashlib.sha256(activation_code.encode("utf-8")).hexdigest()


def _load_disk_cache() -> dict[str, dict]:
    try:
        if not _DISK_CACHE_FILE.is_file():
            return {}
        return json.loads(_DISK_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.warning("activation disk cache read failed: %s", e)
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
    except Exception as e:  # noqa: BLE001 — cache is best-effort
        logger.warning("activation disk cache write failed: %s", e)


def activate(base_url: str, activation_code: str) -> tuple[str, str]:
    """Redeem a single-use Activation Code (act_) into a Script credential.

    Returns (client_id=crd_<uuid>, client_secret=aks_<plain>). The server
    consumes the code; aks_ is returned exactly once.
    """
    resp = httpx.post(
        f"{base_url}/auth/redeem-activation-code",
        json={"code": activation_code},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    return body["client_id"], body["client_secret"]


def resolve_credentials_from_cache(base_url: str) -> tuple[str, str] | None:
    """Return cached (client_id, client_secret) for base_url, or None.

    Lets the runtime work off a previously-redeemed credential without
    re-providing the (now-consumed) Activation Code.
    """
    cached = ACTIVATED_CACHE.get(base_url)
    if cached:
        return cached
    for entry in _load_disk_cache().values():
        if entry.get("base_url") == base_url and entry.get("client_id"):
            pair = (entry["client_id"], entry["client_secret"])
            ACTIVATED_CACHE[base_url] = pair
            return pair
    return None


def validate_activation(base_url: str, activation_code: str) -> tuple[str, str]:
    """Redeem the Activation Code, idempotent via fingerprint cache.

    Re-saving the same code resolves from cache instead of hitting the
    single-use server endpoint again.
    """
    fp = _ac_fingerprint(activation_code)
    cache = _load_disk_cache()
    entry = cache.get(fp)
    if entry and entry.get("base_url") == base_url and entry.get("client_id"):
        pair = (entry["client_id"], entry["client_secret"])
        ACTIVATED_CACHE[base_url] = pair
        return pair
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


def resolve_access_token(creds: dict) -> tuple[str, str]:
    """Resolve (base_url, access_token) from runtime.credentials.

    Order:
      1. OAuth path: Dify supplies (and auto-refreshes) access_token.
      2. Activation Code path: redeem act_ -> Script credential (crd_/aks_),
         idempotent via the activation-code fingerprint cache, then
         client_credentials.
      3. Back-compat: an explicit Script credential (crd_/aks_) supplied
         directly in this credential set.

    Note: there is intentionally NO "resolve any cached credential for this
    base_url" fallback — that could hand a tool the wrong Script's credential
    when several Scripts share one API host (Codex impl review Q2). The AC
    fingerprint cache (validate_activation) keys per-code, so the primary path
    stays correct without it.
    """
    base_url = creds.get("base_url") or "https://api.agentrux.com"
    if not _is_url_allowed(base_url):
        raise ValueError(f"base_url must use HTTPS (got {base_url!r})")

    access_token = creds.get("access_token") or ""
    if access_token:
        return base_url, access_token

    activation_code = creds.get("activation_code") or ""
    if activation_code:
        client_id, client_secret = validate_activation(base_url, activation_code)
        return base_url, _client_credentials_token(base_url, client_id, client_secret)

    client_id = creds.get("client_id") or ""
    client_secret = creds.get("client_secret") or ""
    if client_id and client_secret:
        return base_url, _client_credentials_token(base_url, client_id, client_secret)

    raise ValueError(
        "No credentials available — connect via OAuth or provide an Activation Code (act_...)"
    )


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
    """Build dynamic-select options for the topic selector.

    Primary: GET /topics returns the caller's accessible topics with
    human-readable names (server-sorted by name). Fallback (older server or
    error): derive id-only options from the JWT scope claim.
    """
    try:
        base_url, token = resolve_access_token(creds)
    except Exception:
        return []

    # Primary: GET /topics (names). Preserve the server-side (name, id) order.
    try:
        resp = httpx.get(
            f"{base_url}/topics",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if resp.status_code == 200:
            options: list[dict] = []
            for item in resp.json().get("items", []):
                if not (set(item.get("actions", [])) & allowed_actions):
                    continue
                topic_id = item.get("topic_id")
                if not topic_id:
                    continue
                label = item.get("display_name") or item.get("name") or topic_id
                options.append({"label": label, "value": topic_id})
            return options
    except Exception:
        # Network error, non-JSON body, or unexpected shape -> fall back to
        # deriving id-only options from the JWT scope claim.
        pass

    # Fallback: id-only options derived from the JWT scope claim.
    seen: set[str] = set()
    options = []
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
