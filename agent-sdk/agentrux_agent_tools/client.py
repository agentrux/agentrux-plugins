"""Minimal async HTTP client for the AgenTrux agent toolkit.

Self-contained: depends only on httpx so the plugin can be installed
without the full server-side `agentrux` package.

Auth modes (resolved by AgenTruxToolkit.create):
  - access_token only: client passes through a pre-issued aat_<JWT>.
  - client_id + client_secret: OAuth 2.1 client_credentials grant on
    POST /oauth/token (form-encoded). Re-issues on 401 / TTL.
  - access_token + refresh_token (from `agentrux login`): expiry triggers
    refresh_token grant; on 4xx we surface AuthExpiredError so callers
    can prompt the user to re-run `agentrux login`.

Endpoint shape (Phase 2 SSOT, 2026-05-16):
  - POST /topics/{top_id}/events                            publish_event
  - GET  /topics/{top_id}/events?limit&order&after&type      list_events
  - GET  /topics/{top_id}/events/{evt_id}                    get_event
  - GET  /topics/{top_id}/events/stream (SSE)                subscribe

`top_<uuid>` prefix is *required* (server-side validator). Plain UUIDs
are rejected so we do not silently 404 mid-run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class AgenTruxError(Exception):
    """Base for plugin-side errors."""


class AuthExpiredError(AgenTruxError):
    """access_token + refresh_token both exhausted. Caller should re-login."""


class _MessageEnvelope:
    """Lightweight surrogate for the SDK's MessageEnvelope.

    Only the fields the plugin tools actually read are exposed. Avoids a
    dependency on the server-side `agentrux` package.
    """

    __slots__ = (
        "event_id", "sequence_no", "timestamp", "type", "payload",
        "payload_ref", "producer_script",
    )

    def __init__(self, raw: dict[str, Any]) -> None:
        self.event_id = raw.get("event_id", "")
        self.sequence_no = raw.get("sequence_number", raw.get("sequence_no"))
        self.timestamp = raw.get("timestamp")
        self.type = raw.get("event_type", raw.get("type", ""))
        self.payload = raw.get("payload")
        self.payload_ref = raw.get("payload_object_id") or raw.get("payload_ref")
        self.producer_script = raw.get("producer_script_id") or raw.get("producer_script")


class AgentRuxClient:
    """Plugin-side AgenTrux HTTP client (OAuth 2.1 client_credentials / device-flow).

    Construct via the toolkit; direct use is also supported for callers
    that already hold a valid access_token (e.g. test fixtures).
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        refresh_token: str | None = None,
        client_id: str = "",
        client_secret: str = "",
        client_id_for_refresh: str = "",
        request_timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self._base_url = base_url.rstrip("/")
        self._access_token = token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._client_id_for_refresh = client_id_for_refresh or client_id
        self._token_expires_at: float = 0.0  # epoch seconds; 0 = unknown
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=request_timeout,
            headers={"User-Agent": "agentrux-agent-tools"},
        )

    # ------------------------------------------------------------------
    # auth
    # ------------------------------------------------------------------
    async def _ensure_token(self) -> str:
        """Return a valid access_token, refreshing or re-issuing as needed."""
        async with self._lock:
            # Fast path: cached and >60s of headroom.
            if self._access_token and self._token_expires_at > time.time() + 60:
                return self._access_token
            if self._access_token and self._token_expires_at == 0:
                # Caller supplied an externally-managed token; trust it.
                return self._access_token

            # refresh_token grant takes priority (device-flow path).
            if self._refresh_token:
                try:
                    await self._issue_refresh()
                    return self._access_token
                except AuthExpiredError:
                    # fall through to client_credentials if available
                    if not (self._client_id and self._client_secret):
                        raise

            if self._client_id and self._client_secret:
                await self._issue_client_credentials()
                return self._access_token

            if self._access_token:
                # No way to refresh; trust the caller's token until 401.
                return self._access_token

            raise AgenTruxError("no credentials available to issue access_token")

    async def _issue_client_credentials(self) -> None:
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }
        r = await self._http.post(
            "/oauth/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise AgenTruxError(
                f"/oauth/token client_credentials failed ({r.status_code}): {r.text}"
            )
        self._consume_token_response(r.json())

    async def _issue_refresh(self) -> None:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        if self._client_id_for_refresh:
            form["client_id"] = self._client_id_for_refresh
        r = await self._http.post(
            "/oauth/token",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            # 4xx on refresh = dead refresh_token, caller must re-login.
            if 400 <= r.status_code < 500:
                self._refresh_token = None
                raise AuthExpiredError(
                    f"refresh_token rejected ({r.status_code}); run `agentrux login`"
                )
            raise AgenTruxError(
                f"/oauth/token refresh failed ({r.status_code}): {r.text}"
            )
        self._consume_token_response(r.json())

    def _consume_token_response(self, body: dict[str, Any]) -> None:
        self._access_token = body.get("access_token", "")
        # Server rotates refresh_tokens single-use (refresh_token grant only).
        new_rt = body.get("refresh_token")
        if new_rt:
            self._refresh_token = new_rt
        try:
            expires_in = int(body.get("expires_in", 600))
        except (TypeError, ValueError):
            expires_in = 600
        self._token_expires_at = time.time() + expires_in

    # ------------------------------------------------------------------
    # request helper
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}"}
        r = await self._http.request(
            method, path, json=json_body, params=params, headers=headers,
        )
        if r.status_code == 401:
            # Force re-issue once; suppresses races between concurrent callers.
            self._token_expires_at = 0
            token = await self._ensure_token()
            headers["Authorization"] = f"Bearer {token}"
            r = await self._http.request(
                method, path, json=json_body, params=params, headers=headers,
            )
        return r

    # ------------------------------------------------------------------
    # data plane
    # ------------------------------------------------------------------
    @staticmethod
    def _require_top_prefix(topic_id: str) -> None:
        if not topic_id.startswith("top_"):
            raise ValueError(
                f"topic_id must start with 'top_' (got {topic_id!r}); the server rejects bare UUIDs"
            )

    async def publish(
        self,
        *,
        topic_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """POST /topics/{top_id}/events. Returns event_id."""
        self._require_top_prefix(topic_id)
        body: dict[str, Any] = {"event_type": event_type, "payload": payload or {}}
        r = await self._request("POST", f"/topics/{topic_id}/events", json_body=body)
        if r.status_code not in (200, 201):
            raise AgenTruxError(f"publish failed ({r.status_code}): {r.text}")
        return r.json().get("event_id", "")

    async def list_events(
        self,
        *,
        topic_id: str,
        limit: int = 20,
        event_type: str | None = None,
        after: str | None = None,
        order: str = "desc",
    ) -> tuple[list[_MessageEnvelope], str | None]:
        """GET /topics/{top_id}/events. Returns (envelopes, next_cursor)."""
        self._require_top_prefix(topic_id)
        params: dict[str, Any] = {"limit": limit, "order": order}
        if event_type:
            params["type"] = event_type
        if after:
            params["after"] = after
        r = await self._request("GET", f"/topics/{topic_id}/events", params=params)
        if r.status_code != 200:
            raise AgenTruxError(f"list_events failed ({r.status_code}): {r.text}")
        data = r.json()
        events = data.get("events") or data.get("items") or []
        cursor = (data.get("next") or {}).get("url") if isinstance(data.get("next"), dict) else None
        return [_MessageEnvelope(e) for e in events], cursor

    async def get_event(self, *, topic_id: str, event_id: str) -> _MessageEnvelope:
        """GET /topics/{top_id}/events/{evt_id}."""
        self._require_top_prefix(topic_id)
        r = await self._request("GET", f"/topics/{topic_id}/events/{event_id}")
        if r.status_code != 200:
            raise AgenTruxError(f"get_event failed ({r.status_code}): {r.text}")
        return _MessageEnvelope(r.json())

    def subscribe(self, *, topic_id: str, mode: str = "sse"):
        """Return an async iterator that yields MessageEnvelope from SSE."""
        if mode != "sse":
            raise ValueError(f"unsupported subscribe mode: {mode!r}")
        self._require_top_prefix(topic_id)
        return _SSESubscription(self, topic_id)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AgentRuxClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()


class _SSESubscription:
    """Async iterator over the SSE event stream of a single topic.

    Yields MessageEnvelope. Each SSE frame's `data:` line is parsed as
    JSON: server may publish either a full event body (Phase 2.4 inline)
    or a `{"event_id": "evt_...", "sequence_number": N}` hint. For the
    hint shape the iterator does a follow-up GET to materialize the
    event so the consumer always sees full payload.
    """

    def __init__(self, client: AgentRuxClient, topic_id: str) -> None:
        self._client = client
        self._topic_id = topic_id
        self._stream_cm: Any | None = None
        self._response: httpx.Response | None = None

    async def __aenter__(self) -> "_SSESubscription":
        token = await self._client._ensure_token()
        self._stream_cm = self._client._http.stream(
            "GET",
            f"/topics/{self._topic_id}/events/stream",
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=5.0),
        )
        self._response = await self._stream_cm.__aenter__()
        if self._response.status_code != 200:
            raise AgenTruxError(
                f"SSE handshake failed ({self._response.status_code}): {await self._response.aread()!r}"
            )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stream_cm is not None:
            await self._stream_cm.__aexit__(*exc)
            self._stream_cm = None
            self._response = None

    def __aiter__(self) -> AsyncIterator[_MessageEnvelope]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[_MessageEnvelope]:
        assert self._response is not None, "use `async with subscription` first"
        current: dict[str, str] = {}
        async for raw_line in self._response.aiter_lines():
            line = raw_line.rstrip("\r\n")
            if not line:
                env = await self._flush(current)
                current = {}
                if env is not None:
                    yield env
                continue
            if line.startswith(":"):
                continue
            if ":" not in line:
                continue
            k, _, v = line.partition(":")
            current[k.strip()] = v.lstrip()

    async def _flush(self, frame: dict[str, str]) -> _MessageEnvelope | None:
        if frame.get("event") != "hint":
            return None
        try:
            data = json.loads(frame.get("data", "{}"))
        except json.JSONDecodeError:
            return None
        # Phase 2.5b SSOT: `data: {"seq": N}` + `id: evt_<uuid>`.
        evt_id = frame.get("id") or data.get("event_id") or ""
        if not evt_id:
            return None
        try:
            return await self._client.get_event(topic_id=self._topic_id, event_id=evt_id)
        except AgenTruxError as e:
            logger.warning("SSE hint follow-up GET failed: %s", e)
            return None
