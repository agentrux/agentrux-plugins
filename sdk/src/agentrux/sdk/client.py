"""Low-level HTTP client for the AgenTrux SDK.

Talks to two endpoint families with two distinct error envelopes:

  * Topic data plane (`/topics/*`, `/.well-known/*`)
      FastAPI HTTPException envelope: `{"detail": {"error": ..., "message": ...}}`
      Bearer token required for /topics/*, none for /.well-known/*.

  * OAuth (`/oauth/*`, `/auth/redeem-activation-code`)
      RFC 6749 §5.2 / RFC 8628 §3.5 flat envelope: `{"error": ..., "error_description": ...}`
      Form-encoded body, no Bearer (the call IS the authentication).

The two are split in `_request_pipe()` vs `_request_oauth()`; callers above
this layer (facade.py) never see the wire format.

SSE is intentionally NOT here — `sse_client.py` opens its own httpx stream
because it needs frame-level access (`event:` / `id:` / `data:`) and a
different reconnection strategy than the normal request-retry loop.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

import httpx

from agentrux.sdk.auth_models import (
    ActivationCodeRedemption,
    AuthorizationServerMetadata,
    DCRRegistration,
    DeviceAuthorization,
    OAuthTokenResponse,
    PayloadDownload,
    PayloadUploadTicket,
)
from agentrux.sdk.envelope import ListEventsPage, MessageEnvelope, PublishResult
from agentrux.sdk.errors import (
    APIError,
    InternalServerError,
    OAuthError,
    SDKError,
    UnauthorizedError,
    api_error_from_detail,
    oauth_error_from_body,
)

logger = logging.getLogger("agentrux.sdk.client")


# ---------------------------------------------------------------------------
# TokenManager — owns access_token / refresh_token + auto-refresh
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenBundle:
    """Snapshot of the auth state at a point in time.

    Carries enough for a persistence layer (e.g. the CLI's credentials
    writer) to reconstruct the next process's state without re-decoding
    the JWT.
    """

    access_token: str
    refresh_token: str | None
    expires_at_unix: int   # absolute unix epoch seconds


TokenRefreshedHook = Callable[[TokenBundle], Awaitable[None] | None]


class TokenRefresher(Protocol):
    """Pluggable JWT refresher.

    The SDK's default refresher (`OAuthRefreshTokenRefresher`) hits
    POST /oauth/token grant_type=refresh_token; callers can swap in a
    custom one (e.g. a long-running daemon that does device_code re-
    authorization out-of-band).
    """

    async def refresh(self, current_refresh_token: str) -> TokenBundle:
        ...


class OAuthRefreshTokenRefresher:
    """RFC 6749 §6 compliant refresher.

    Requires `oauth_client_id` because §6 mandates `client_id` in the
    request when the client is public (the device-flow / DCR case). For
    confidential clients (`crd_<uuid>` + `aks_<base64>`) the
    `client_secret` is also sent.
    """

    def __init__(
        self,
        *,
        base_url: str,
        oauth_client_id: str,
        client_secret: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not oauth_client_id:
            raise ValueError(
                "OAuthRefreshTokenRefresher requires oauth_client_id "
                "(RFC 6749 §6 mandates client_id on refresh_token grant)."
            )
        self._base_url = base_url.rstrip("/")
        self._client_id = oauth_client_id
        self._client_secret = client_secret
        self._http = http
        self._owns_http = http is None

    async def __aenter__(self) -> OAuthRefreshTokenRefresher:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def refresh(self, current_refresh_token: str) -> TokenBundle:
        body = {
            "grant_type": "refresh_token",
            "refresh_token": current_refresh_token,
            "client_id": self._client_id,
        }
        if self._client_secret:
            body["client_secret"] = self._client_secret
        http = self._http or httpx.AsyncClient(timeout=30.0)
        try:
            resp = await http.post(
                f"{self._base_url}/oauth/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        finally:
            if self._http is None:
                await http.aclose()
        if resp.status_code != 200:
            _raise_oauth_error(resp)
        data = resp.json()
        otoken = OAuthTokenResponse.from_response(data)
        # Server SHOULD rotate refresh_token (single-use); if it doesn't,
        # keep the old one (RFC 6749 §6 permits reuse).
        return TokenBundle(
            access_token=otoken.access_token,
            refresh_token=otoken.refresh_token or current_refresh_token,
            expires_at_unix=int(time.time()) + otoken.expires_in,
        )


class TokenManager:
    """Owns the current access_token and serializes refreshes.

    Concurrent calls to ensure_valid() coalesce on a single
    `_refresh_lock` so we never issue two refresh requests in flight at
    the same time. Without the lock, every async call that arrives at
    the same moment would try to refresh, racing the server's single-
    use refresh_token rotation and losing the family.
    """

    REFRESH_THRESHOLD_SECONDS = 60  # refresh if <= this many seconds left

    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
        refresher: TokenRefresher | None = None,
        on_refreshed: TokenRefreshedHook | None = None,
        explicit_expires_at_unix: int | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._refresher = refresher
        self._on_refreshed = on_refreshed
        self._expires_at_unix = (
            explicit_expires_at_unix
            if explicit_expires_at_unix is not None
            else _decode_jwt_exp(access_token)
        )
        self._refresh_lock = asyncio.Lock()

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def expires_at_unix(self) -> int | None:
        return self._expires_at_unix

    def bundle(self) -> TokenBundle:
        return TokenBundle(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            expires_at_unix=self._expires_at_unix or 0,
        )

    async def ensure_valid(self) -> None:
        """If the JWT is within REFRESH_THRESHOLD_SECONDS of expiring, refresh now."""
        if self._expires_at_unix is None:
            # No exp claim and no explicit expiry → cannot refresh proactively.
            return
        remaining = self._expires_at_unix - int(time.time())
        if remaining > self.REFRESH_THRESHOLD_SECONDS:
            return
        await self._do_refresh()

    async def force_refresh(self) -> None:
        """Refresh regardless of remaining TTL (e.g. on a 401 response).

        Unlike `ensure_valid()`, this bypasses the "still has TTL left"
        early return — the server already told us the token is invalid,
        so we refresh even if local clock thinks it's fresh.
        """
        await self._do_refresh(force=True)

    async def _do_refresh(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            # Re-check inside the lock: another coroutine may have just
            # refreshed while we were waiting. Skip if a recent refresh
            # already pushed expiry beyond the threshold — unless force=True
            # (a 401 from the server takes precedence over local clock).
            if not force and self._expires_at_unix is not None:
                remaining = self._expires_at_unix - int(time.time())
                if remaining > self.REFRESH_THRESHOLD_SECONDS:
                    return
            if self._refresher is None:
                # No refresher at all; let the next request 401 naturally.
                return
            # The refresher's `refresh()` accepts a refresh_token string.
            # For the client_credentials path we don't have one (the
            # OAuthTokenResponse omits refresh_token by spec), but the
            # _ClientCredentialsRefresher ignores it and just re-runs
            # the credentials exchange. So we pass empty string rather
            # than None, and the per-refresher contract decides what to
            # do with it. Refusing to call the refresher just because
            # refresh_token is None would break client_credentials
            # auto-renewal (Codex 2nd review #1).
            new_bundle = await self._refresher.refresh(self._refresh_token or "")
            self._access_token = new_bundle.access_token
            self._refresh_token = new_bundle.refresh_token
            self._expires_at_unix = new_bundle.expires_at_unix
            if self._on_refreshed is not None:
                result = self._on_refreshed(new_bundle)
                if result is not None:
                    await result


def _decode_jwt_exp(token: str) -> int | None:
    """Extract the `exp` claim from a JWT without signature verification.

    Returns None if the token isn't a JWT or lacks an exp claim. Used
    only for client-side refresh-timing decisions; we never trust this
    value for authorization (the server re-verifies on every call).
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return int(exp) if exp is not None else None
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):
        return None


# ---------------------------------------------------------------------------
# Pipe error envelope parser (FastAPI HTTPException shape)
# ---------------------------------------------------------------------------


def _raise_pipe_error(resp: httpx.Response) -> None:
    """Raise an APIError subclass from a pipe error response."""
    status = resp.status_code
    request_id = resp.headers.get("X-Request-Id")
    try:
        body = resp.json()
    except ValueError:
        # Non-JSON body — surface only HTTP status + server's correlation
        # id (if any). Do NOT include the raw body: it may contain leaked
        # internals (per CLAUDE.md §publish safety rule e).
        raise InternalServerError(
            f"unparseable error body (HTTP {status}"
            + (f", request_id={request_id}" if request_id else "")
            + ")",
            status_code=status,
            code="INTERNAL",
        )
    # FastAPI wraps detail as {"detail": <original>}; the server's detail
    # dict carries the typed envelope.
    detail = body.get("detail", body)
    if not isinstance(detail, dict):
        # Some 4xx (e.g. Pydantic validation 422) returns
        # {"detail": [{"loc": ..., "msg": ...}, ...]} which we surface raw.
        raise APIError(
            f"HTTP {status}: {detail!r}",
            status_code=status,
            code="INVALID" if 400 <= status < 500 else "INTERNAL",
        )
    raise api_error_from_detail(status_code=status, detail=detail)


def _raise_oauth_error(resp: httpx.Response) -> None:
    """Raise an OAuthError subclass from a RFC 6749 error response."""
    status = resp.status_code
    request_id = resp.headers.get("X-Request-Id")
    try:
        body = resp.json()
    except ValueError:
        raise OAuthError(
            f"unparseable OAuth error (HTTP {status}"
            + (f", request_id={request_id}" if request_id else "")
            + ")",
            status_code=status,
            error="invalid_request",
        )
    if not isinstance(body, dict):
        raise OAuthError(
            f"unexpected OAuth error shape (HTTP {status})",
            status_code=status,
            error="invalid_request",
        )
    raise oauth_error_from_body(status_code=status, body=body)


# ---------------------------------------------------------------------------
# AgenTruxAPIClient — pipe + OAuth + DCR + device + AC, in one class
# ---------------------------------------------------------------------------


class AgenTruxAPIClient:
    """Low-level HTTP client.

    Owned by `facade.AgenTruxClient`; not normally instantiated directly
    by application code. Holds a `TokenManager` for /topics/* calls; the
    OAuth/DCR/device methods are static (or take their own credentials)
    because they precede token issuance.
    """

    MAX_RETRIES_SERVER_ERROR = 2  # retry 5xx up to this many times
    INITIAL_BACKOFF_SECONDS = 0.5

    def __init__(
        self,
        *,
        base_url: str,
        token_manager: TokenManager | None = None,
        timeout_seconds: float = 30.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        # Setting base_url on the default client lets sse_client.py use a
        # relative path ("/topics/{id}/events/stream") and have it resolve
        # against this client, which is what makes MockTransport (in tests)
        # — and any per-process httpx settings — work for SSE too.
        self._http = http or httpx.AsyncClient(
            timeout=timeout_seconds, base_url=self._base_url
        )
        self._owns_http = http is None

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def token_manager(self) -> TokenManager | None:
        return self._token_manager

    async def __aenter__(self) -> AgenTruxAPIClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    # --- internal: pipe (Bearer) request with retry + auto-refresh ----------

    async def _request_pipe(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        require_auth: bool = True,
    ) -> httpx.Response:
        """Issue a pipe request, handling 401-refresh-retry and 5xx-backoff.

        Authenticated calls go through the TokenManager: if the JWT is
        within REFRESH_THRESHOLD_SECONDS of expiry, refresh BEFORE
        sending (avoids the server's clock-skew 401). On a 401 anyway,
        force one refresh and retry once.
        """
        url = f"{self._base_url}{path}"
        merged_headers: dict[str, str] = dict(headers or {})
        if require_auth:
            if self._token_manager is None:
                raise SDKError(
                    f"{method} {path} requires a Bearer token but no "
                    "TokenManager was configured."
                )
            await self._token_manager.ensure_valid()
            merged_headers["Authorization"] = f"Bearer {self._token_manager.access_token}"

        attempt = 0
        backoff = self.INITIAL_BACKOFF_SECONDS
        already_refreshed_on_401 = False
        while True:
            try:
                resp = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=merged_headers,
                )
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                if attempt >= self.MAX_RETRIES_SERVER_ERROR:
                    raise SDKError(
                        f"{method} {url} failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                attempt += 1
                backoff *= 2
                continue

            if resp.status_code < 400:
                return resp

            # 401 with auth → try one refresh-and-retry, then surface.
            if (
                resp.status_code == 401
                and require_auth
                and self._token_manager is not None
                and not already_refreshed_on_401
            ):
                already_refreshed_on_401 = True
                await self._token_manager.force_refresh()
                merged_headers["Authorization"] = (
                    f"Bearer {self._token_manager.access_token}"
                )
                continue

            # 5xx → bounded retry with exponential backoff.
            if 500 <= resp.status_code < 600 and attempt < self.MAX_RETRIES_SERVER_ERROR:
                attempt += 1
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            # Surface as APIError (pipe envelope).
            _raise_pipe_error(resp)
            # _raise_pipe_error always raises; unreachable.
            raise UnauthorizedError(  # pragma: no cover
                "unreachable", status_code=resp.status_code, code="INTERNAL"
            )

    # --- internal: OAuth request (form-encoded, no Bearer) ------------------

    async def _request_oauth(
        self,
        path: str,
        body_form: dict[str, str],
    ) -> httpx.Response:
        """POST a form-encoded OAuth request and parse RFC 6749 errors."""
        resp = await self._http.post(
            f"{self._base_url}{path}",
            data=body_form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            _raise_oauth_error(resp)
        return resp

    async def _request_json(
        self, method: str, path: str, *, json_body: Any | None = None
    ) -> httpx.Response:
        """JSON-body request without Bearer (used by DCR, AC redeem)."""
        resp = await self._http.request(
            method,
            f"{self._base_url}{path}",
            json=json_body,
        )
        return resp

    # =======================================================================
    # OAuth 2.1 (POST /oauth/token)
    # =======================================================================

    async def oauth_token_client_credentials(
        self, *, client_id: str, client_secret: str, scope: str | None = None
    ) -> OAuthTokenResponse:
        """Exchange `crd_<uuid>` + `aks_<base64>` for an access_token.

        client_credentials does not issue a refresh_token (Phase 1.9b
        spec line 232); short-lived JWT only.
        """
        if not client_id.startswith("crd_"):
            raise ValueError(
                f"client_id must start with 'crd_' (Phase 1.9 credential "
                f"prefix), got {client_id!r}"
            )
        body = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if scope:
            body["scope"] = scope
        resp = await self._request_oauth("/oauth/token", body)
        return OAuthTokenResponse.from_response(resp.json())

    async def oauth_token_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        client_secret: str | None = None,
    ) -> OAuthTokenResponse:
        """PKCE authorization_code exchange (Phase 1.4)."""
        body = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
        if client_secret:
            body["client_secret"] = client_secret
        resp = await self._request_oauth("/oauth/token", body)
        return OAuthTokenResponse.from_response(resp.json())

    async def oauth_token_refresh(
        self,
        *,
        refresh_token: str,
        client_id: str,
        client_secret: str | None = None,
    ) -> OAuthTokenResponse:
        """RFC 6749 §6 refresh — client_id is mandatory."""
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            body["client_secret"] = client_secret
        resp = await self._request_oauth("/oauth/token", body)
        return OAuthTokenResponse.from_response(resp.json())

    async def oauth_token_device_code(
        self,
        *,
        device_code: str,
        client_id: str,
    ) -> OAuthTokenResponse:
        """RFC 8628 device_code grant — polled by the device flow caller."""
        body = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        }
        resp = await self._request_oauth("/oauth/token", body)
        return OAuthTokenResponse.from_response(resp.json())

    # =======================================================================
    # RFC 7591 DCR (POST /oauth/register)
    # =======================================================================

    async def register_dcr(
        self,
        *,
        client_name: str,
        redirect_uris: list[str] | None = None,
        grant_types: list[str] | None = None,
    ) -> DCRRegistration:
        """Register a public OAuth client for device flow / MCP.

        Server only accepts `token_endpoint_auth_method='none'` (the
        only kind of public client it issues); the SDK hardcodes this.
        """
        body = {
            "client_name": client_name,
            "redirect_uris": redirect_uris or [],
            "grant_types": grant_types or ["device_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        }
        resp = await self._request_json("POST", "/oauth/register", json_body=body)
        if resp.status_code != 201:
            _raise_oauth_error(resp)
        return DCRRegistration.from_response(resp.json())

    # =======================================================================
    # RFC 8628 Device flow (POST /oauth/device/authorize)
    # =======================================================================

    async def device_authorization(
        self, *, client_id: str, scope: str | None = None
    ) -> DeviceAuthorization:
        body = {"client_id": client_id}
        if scope:
            body["scope"] = scope
        resp = await self._request_oauth("/oauth/device/authorize", body)
        return DeviceAuthorization.from_response(resp.json())

    # =======================================================================
    # Activation Code (POST /auth/redeem-activation-code, Phase 1.9)
    # =======================================================================

    async def redeem_activation_code(
        self, *, code: str
    ) -> ActivationCodeRedemption:
        """One-shot redeem an `act_<base64>` code → (crd_, aks_, scr_).

        Body is JSON `{"code": "<act_...>"}` (activation_code_router.py:47).
        """
        if not code.startswith("act_"):
            raise ValueError(
                f"activation code must start with 'act_', got {code!r}"
            )
        resp = await self._request_json(
            "POST", "/auth/redeem-activation-code", json_body={"code": code}
        )
        if resp.status_code != 200:
            # AC redeem uses the pipe envelope (it's behind FastAPI HTTPException).
            _raise_pipe_error(resp)
        return ActivationCodeRedemption.from_response(resp.json())

    # =======================================================================
    # RFC 8414 metadata discovery
    # =======================================================================

    async def discover_metadata(self) -> AuthorizationServerMetadata:
        """GET /.well-known/oauth-authorization-server."""
        resp = await self._http.get(
            f"{self._base_url}/.well-known/oauth-authorization-server"
        )
        if resp.status_code != 200:
            _raise_pipe_error(resp)
        return AuthorizationServerMetadata.from_response(resp.json())

    async def get_jwks(self) -> dict[str, Any]:
        """GET /.well-known/jwks.json — server's RSA public keys (RFC 7517).

        Returns the raw JWKS dict (`{"keys": [...]}`) for callers that
        want to verify AgenTrux-issued JWTs locally rather than calling
        /oauth/introspect. The SDK itself never verifies JWTs locally
        (the server is authoritative on every request), but exposing
        this lets, e.g., a downstream service that receives a forwarded
        `aat_` validate it without a round-trip.
        """
        resp = await self._http.get(f"{self._base_url}/.well-known/jwks.json")
        if resp.status_code != 200:
            _raise_pipe_error(resp)
        body = resp.json()
        if not isinstance(body, dict):
            raise SDKError(f"jwks must be JSON object, got {type(body).__name__}")
        return body

    # =======================================================================
    # Topic data plane: publish / get / list / payloads
    # =======================================================================

    async def publish_event(
        self,
        *,
        topic_id: str,
        event_type: str | None = None,
        payload: Any = None,
        payload_object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> PublishResult:
        """POST /topics/{top_id}/events (Phase 2.2 / 2.4)."""
        _require_topic_prefix(topic_id)
        # Server validates inline vs object_ref exclusivity; we just
        # forward both fields and let the server decide.
        body: dict[str, Any] = {"event_type": event_type}
        if payload is not None:
            body["payload"] = payload
        if payload_object_id is not None:
            if not payload_object_id.startswith("pob_"):
                raise ValueError(
                    f"payload_object_id must start with 'pob_', "
                    f"got {payload_object_id!r}"
                )
            body["payload_object_id"] = payload_object_id
        if metadata is not None:
            body["metadata"] = metadata

        headers: dict[str, str] = {}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        resp = await self._request_pipe(
            "POST",
            f"/topics/{topic_id}/events",
            json_body=body,
            headers=headers or None,
        )
        return PublishResult.from_response(resp.json())

    async def get_event(self, *, topic_id: str, event_id: str) -> MessageEnvelope:
        """GET /topics/{top_id}/events/{evt_id}."""
        _require_topic_prefix(topic_id)
        if not event_id.startswith("evt_"):
            raise ValueError(
                f"event_id must start with 'evt_', got {event_id!r}"
            )
        resp = await self._request_pipe(
            "GET", f"/topics/{topic_id}/events/{event_id}"
        )
        return MessageEnvelope.from_event_view(resp.json())

    async def list_events(
        self,
        *,
        topic_id: str,
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
        order: str = "asc",
        event_type: str | None = None,
        expand: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> ListEventsPage:
        """GET /topics/{top_id}/events with cursor pagination (Phase 2.5)."""
        _require_topic_prefix(topic_id)
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")
        if order not in ("asc", "desc"):
            raise ValueError(f"order must be 'asc' or 'desc', got {order!r}")
        if after is not None and not after.startswith("evt_"):
            raise ValueError(f"after must start with 'evt_', got {after!r}")
        if before is not None and not before.startswith("evt_"):
            raise ValueError(f"before must start with 'evt_', got {before!r}")

        params: dict[str, Any] = {"limit": limit, "order": order}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        if event_type is not None:
            params["type"] = event_type
        if expand is not None:
            params["expand"] = expand
        if since is not None:
            params["since"] = since
        if until is not None:
            params["until"] = until

        resp = await self._request_pipe(
            "GET", f"/topics/{topic_id}/events", params=params
        )
        clamped = resp.headers.get("X-AgenTrux-Pagination") == "clamped"
        return ListEventsPage.from_response(resp.json(), clamped=clamped)

    # --- Payload object plane (POST/GET /topics/{id}/payloads) --------------

    async def request_payload_upload(
        self,
        *,
        topic_id: str,
        content_type: str | None = None,
        size_bytes: int | None = None,
        checksum_sha256: str | None = None,
    ) -> PayloadUploadTicket:
        """POST /topics/{top_id}/payloads (Phase 2.4a presigned PUT URL)."""
        _require_topic_prefix(topic_id)
        body: dict[str, Any] = {}
        if content_type is not None:
            body["content_type"] = content_type
        if size_bytes is not None:
            body["size_bytes"] = size_bytes
        if checksum_sha256 is not None:
            body["checksum_sha256"] = checksum_sha256
        resp = await self._request_pipe(
            "POST", f"/topics/{topic_id}/payloads", json_body=body or None
        )
        if resp.status_code not in (200, 201):
            _raise_pipe_error(resp)
        return PayloadUploadTicket.from_response(resp.json())

    async def get_payload(
        self, *, topic_id: str, payload_object_id: str
    ) -> PayloadDownload:
        """GET /topics/{top_id}/payloads/{pob_id} (Phase 2.4c)."""
        _require_topic_prefix(topic_id)
        if not payload_object_id.startswith("pob_"):
            raise ValueError(
                f"payload_object_id must start with 'pob_', "
                f"got {payload_object_id!r}"
            )
        resp = await self._request_pipe(
            "GET", f"/topics/{topic_id}/payloads/{payload_object_id}"
        )
        return PayloadDownload.from_response(resp.json())

    async def put_payload_bytes(
        self,
        *,
        upload_url: str,
        content: bytes,
        required_headers: dict[str, str] | None = None,
    ) -> None:
        """PUT bytes to a presigned upload URL (S3/MinIO direct)."""
        headers: dict[str, str] = dict(required_headers or {})
        # Use a fresh client (no Authorization header — the URL is signed)
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.put(upload_url, content=content, headers=headers)
            if resp.status_code not in (200, 201, 204):
                # Don't surface the body: presigned URL responses often
                # include backend tokens / signatures (CLAUDE.md §publish
                # safety rule e).
                raise SDKError(f"presigned PUT failed: HTTP {resp.status_code}")


def _require_topic_prefix(topic_id: str) -> None:
    if not topic_id.startswith("top_"):
        raise ValueError(
            f"topic_id must start with 'top_', got {topic_id!r}"
        )
