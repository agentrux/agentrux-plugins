"""AgenTruxAPIClient - HTTP client with auth, retry, and JWT refresh."""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import httpx

from agentrux.sdk.errors import TokenExpiredError

logger = logging.getLogger("agentrux.sdk.client")


@dataclass(frozen=True)
class TokenBundle:
    """The full payload of a successful refresh: access + refresh + expiry.

    Carries enough state for a persistence layer (e.g. the ``agentrux``
    CLI's ``~/.agentrux/credentials`` writer) to rebuild what the next
    process needs without re-decoding the JWT.
    """

    access_token: str
    refresh_token: str
    expires_at: int  # unix epoch seconds


# ``on_token_refreshed`` may be sync or async. We await the result either
# way, so a sync callable just needs to return a non-awaitable.
TokenRefreshedHook = Callable[[TokenBundle], Awaitable[None] | None]


class TokenRefresher(Protocol):
    """Protocol for JWT auto-refresh implementations.

    Implementations exchange the long-lived ``refresh_token`` for a new
    ``access_token`` (and possibly a rotated ``refresh_token``) and
    return the result as a ``TokenBundle``. Returning the bundle rather
    than a tuple keeps the contract self-describing as we add fields
    (``expires_at`` already, ``scope`` later) without breaking callers.
    """

    async def refresh(self, current_token: str, refresh_token: str) -> TokenBundle:
        ...


class DefaultTokenRefresher:
    """OAuth 2.1 refresh: POST /oauth/token with grant_type=refresh_token.

    Replaces the old `/auth/refresh` JSON endpoint. Per RFC 6749 §6 and
    AgenTrux spec §22.2 (line 185-212), the body MUST be
    ``application/x-www-form-urlencoded`` and MUST include ``client_id``.
    Public DCR clients (``oauth-client_<uuid>``, the device-flow CLI
    case) authenticate with no client_secret — PKCE / DCR proves
    identity. Server rotates the refresh_token on every successful call
    (single-use); the new value is in the response body.
    """

    def __init__(self, base_url: str, oauth_client_id: str):
        if not oauth_client_id:
            # Caller error — caught here so we surface it before the
            # network round-trip.
            raise ValueError(
                "DefaultTokenRefresher requires oauth_client_id (the DCR-"
                "registered client_id, e.g. 'oauth-client_<uuid>'). "
                "client_credentials grants do not issue refresh tokens "
                "and so do not need a refresher."
            )
        self._base_url = base_url.rstrip("/")
        self._oauth_client_id = oauth_client_id

    async def refresh(self, current_token: str, refresh_token: str) -> TokenBundle:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self._oauth_client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            access = data["access_token"]
            # Server SHOULD rotate refresh_token; if it omits the field
            # (RFC 6749 §6 allows reuse), keep the previous one.
            new_refresh = data.get("refresh_token") or refresh_token
            expires_in = int(data.get("expires_in", 3600) or 3600)
            return TokenBundle(
                access_token=access,
                refresh_token=new_refresh,
                expires_at=int(time.time()) + expires_in,
            )


class AgenTruxAPIClient:
    """HTTP client for AgenTrux API with authentication and retry."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        refresh_token: str | None = None,
        oauth_client_id: str | None = None,
        token_refresher: TokenRefresher | None = None,
        on_token_refreshed: TokenRefreshedHook | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        max_payload_bytes: int = 10 * 1024 * 1024,  # 10MB
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._refresh_token = refresh_token
        self._oauth_client_id = oauth_client_id
        # The default refresher requires oauth_client_id (per OAuth 2.1
        # spec §6 — refresh_token grant MUST carry client_id). If the
        # caller passed a refresh_token without an oauth_client_id we
        # leave the refresher unset rather than build a broken default
        # that would fail on first refresh; the caller can either supply
        # a custom refresher or accept that the access_token is
        # short-lived (e.g. a Path-1 explicit access_token use).
        if token_refresher is not None:
            self._token_refresher: TokenRefresher | None = token_refresher
        elif refresh_token and oauth_client_id:
            self._token_refresher = DefaultTokenRefresher(base_url, oauth_client_id)
        else:
            self._token_refresher = None
            if refresh_token and not oauth_client_id:
                logger.warning(
                    "AgenTruxAPIClient received refresh_token but no "
                    "oauth_client_id; auto-refresh disabled. Pass "
                    "oauth_client_id= or supply a custom token_refresher."
                )
        self._on_token_refreshed = on_token_refreshed
        self._timeout = timeout_s
        self._max_retries = max_retries
        self._max_payload_bytes = max_payload_bytes
        self._client = httpx.AsyncClient(timeout=timeout_s)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _ensure_valid_token(self) -> None:
        """Check JWT expiration and refresh if needed."""
        try:
            # Decode JWT payload without verification (just check exp)
            parts = self._token.split(".")
            if len(parts) != 3:
                return
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp")
            if exp is None:
                return

            remaining = exp - time.time()
            if remaining > 60:  # More than 60s remaining
                return

            # Need refresh
            if self._token_refresher and self._refresh_token:
                logger.info("JWT expiring in %.0fs, refreshing...", remaining)
                bundle = await self._token_refresher.refresh(
                    self._token, self._refresh_token
                )
                self._token = bundle.access_token
                self._refresh_token = bundle.refresh_token
                if self._on_token_refreshed is not None:
                    res = self._on_token_refreshed(bundle)
                    if res is not None:
                        await res
                logger.info("JWT refreshed successfully")
            elif remaining <= 0:
                raise TokenExpiredError("JWT expired and no refresh mechanism available")

        except TokenExpiredError:
            raise
        except Exception as e:
            logger.warning("Token validation check failed: %s", e)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Make authenticated request with retry."""
        await self._ensure_valid_token()

        last_error = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers=self._headers(),
                    **kwargs,
                )
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401 and self._token_refresher and self._refresh_token:
                    try:
                        bundle = await self._token_refresher.refresh(
                            self._token, self._refresh_token
                        )
                        self._token = bundle.access_token
                        self._refresh_token = bundle.refresh_token
                        if self._on_token_refreshed is not None:
                            res = self._on_token_refreshed(bundle)
                            if res is not None:
                                await res
                        continue
                    except Exception:
                        raise TokenExpiredError("Token refresh failed") from e
                if e.response.status_code < 500:
                    raise
                last_error = e
            except (httpx.ConnectError, httpx.ReadTimeout) as e:
                last_error = e

        raise last_error  # type: ignore[misc]

    async def list_events(
        self,
        topic_id: str,
        cursor: str | None = None,
        limit: int = 50,
        event_type: str | None = None,
        after_sequence_no: int | None = None,
    ) -> tuple[list[dict], str | None]:
        """GET /topics/{topic_id}/events. Returns (items, next_cursor).

        cursor and after_sequence_no are mutually exclusive — pass only one.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if after_sequence_no is not None:
            params["after_sequence_no"] = after_sequence_no
        if event_type:
            params["type"] = event_type

        resp = await self._request("GET", f"/topics/{topic_id}/events", params=params)
        data = resp.json()
        return data.get("items", []), data.get("next_cursor")

    async def list_events_by_sequence(
        self, topic_id: str, start_seq: int, end_seq: int,
    ) -> list[dict]:
        """GET /topics/{topic_id}/events/by-sequence?start_seq=&end_seq=.

        For GapDetector backfill. Server enforces max range of 500.
        Returns items ordered by sequence_no ascending. A short response
        (count < end_seq - start_seq + 1) means some sequences in the
        range have been physically deleted by the retention cleanup job.
        """
        resp = await self._request(
            "GET", f"/topics/{topic_id}/events/by-sequence",
            params={"start_seq": start_seq, "end_seq": end_seq},
        )
        return resp.json().get("items", [])

    async def get_event(self, topic_id: str, event_id: str) -> dict:
        """GET /topics/{topic_id}/events/{event_id}."""
        resp = await self._request("GET", f"/topics/{topic_id}/events/{event_id}")
        return resp.json()

    async def publish_event(
        self,
        topic_id: str,
        type: str,
        payload: dict | None = None,
        payload_ref: str | None = None,
    ) -> str:
        """POST /topics/{topic_id}/events. Returns event_id."""
        body: dict[str, Any] = {"type": type}
        if payload is not None:
            body["payload"] = payload
        if payload_ref is not None:
            body["payload_ref"] = payload_ref

        resp = await self._request("POST", f"/topics/{topic_id}/events", json=body)
        return resp.json()["event_id"]

    async def connect_sse(
        self,
        topic_id: str,
        last_event_id: int | None = None,
    ) -> AsyncIterator[tuple[int | None, dict]]:
        """Connect to SSE stream. Yields (sequence_no, event_data) tuples."""
        await self._ensure_valid_token()

        headers = self._headers()
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)

        async with httpx.AsyncClient(timeout=None) as stream_client:
            async with stream_client.stream(
                "GET",
                f"{self._base_url}/topics/{topic_id}/events/stream",
                headers=headers,
            ) as response:
                response.raise_for_status()
                current_id: int | None = None
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("id: "):
                        try:
                            current_id = int(line[4:])
                        except ValueError:
                            current_id = None
                    elif line.startswith("data: "):
                        try:
                            data = json.loads(line[6:])
                            yield current_id, data
                        except json.JSONDecodeError:
                            logger.warning("Invalid SSE JSON: %s", line[6:50])
                        current_id = None

    async def __aenter__(self) -> "AgenTruxAPIClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    @property
    def token(self) -> str:
        return self._token

    async def close(self) -> None:
        await self._client.aclose()

    # --- Auth helpers (unauthenticated, used before token is available) ---

    @staticmethod
    async def auth_request(base_url: str, path: str, body: dict) -> dict:
        """POST an unauthenticated JSON request to an auth endpoint. Returns JSON."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url.rstrip('/')}{path}", json=body)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    async def oauth_token(
        base_url: str, script_id: str, client_secret: str,
    ) -> dict:
        """Exchange script credentials for an access token via OAuth 2.1
        client_credentials grant. POST /oauth/token (form-encoded).

        client_id is the script identifier with the ``script_`` prefix
        (added automatically here if the caller passes the bare UUID).

        Returns the standard OAuth response dict::

            {
                "access_token": "...",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "...",   # optional
            }
        """
        cid = script_id if script_id.startswith("script_") else f"script_{script_id}"
        data = {
            "grant_type": "client_credentials",
            "client_id": cid,
            "client_secret": client_secret,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/oauth/token",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return resp.json()
