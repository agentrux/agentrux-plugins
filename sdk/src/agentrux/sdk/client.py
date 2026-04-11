"""AgenTruxAPIClient - HTTP client with auth, retry, and JWT refresh."""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any, AsyncIterator, Protocol

import httpx

from agentrux.sdk.errors import TokenExpiredError

logger = logging.getLogger("agentrux.sdk.client")


class TokenRefresher(Protocol):
    """Protocol for JWT auto-refresh implementations."""

    async def refresh(self, current_token: str, refresh_token: str) -> tuple[str, str]:
        """Return new (access_token, refresh_token)."""
        ...


class DefaultTokenRefresher:
    """Standard AgenTrux refresh endpoint: POST /auth/refresh."""

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")

    async def refresh(self, current_token: str, refresh_token: str) -> tuple[str, str]:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/auth/refresh",
                json={"refresh_token": refresh_token},
                headers={"Authorization": f"Bearer {current_token}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["access_token"], data["refresh_token"]


class AgenTruxAPIClient:
    """HTTP client for AgenTrux API with authentication and retry."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        refresh_token: str | None = None,
        token_refresher: TokenRefresher | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        max_payload_bytes: int = 10 * 1024 * 1024,  # 10MB
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._refresh_token = refresh_token
        self._token_refresher = token_refresher or (
            DefaultTokenRefresher(base_url) if refresh_token else None
        )
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
                new_token, new_refresh = await self._token_refresher.refresh(
                    self._token, self._refresh_token
                )
                self._token = new_token
                self._refresh_token = new_refresh
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
                        self._token, self._refresh_token = await self._token_refresher.refresh(
                            self._token, self._refresh_token
                        )
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
        """POST an unauthenticated request to an auth endpoint. Returns JSON."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{base_url.rstrip('/')}{path}", json=body)
            resp.raise_for_status()
            return resp.json()
