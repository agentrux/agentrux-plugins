"""High-level public API of the AgenTrux Python SDK.

Centralizes the entry points the application code is expected to touch
directly:

  * `AgenTruxClient`           — the connected client (token-backed)
  * `AgenTruxClient.from_*`    — factories for the supported credential paths
  * `AgenTruxClient.publish`   — POST /topics/.../events
  * `AgenTruxClient.list_events`, `.get_event`, `.upload_payload`, `.get_payload`
  * `AgenTruxClient.subscribe` — long-running consumer (pull / sse / hybrid)
  * `connect`                  — convenience for the common cases

Everything below this layer (httpx, retry, refresh, SSE frames) lives in
client.py / pull_client.py / sse_client.py / hybrid_consumer.py.

This module is the SDK surface contract; breaking changes here drive
the package's MAJOR version.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

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
from agentrux.sdk.checkpoint import CheckpointStore
from agentrux.sdk.client import (
    AgenTruxAPIClient,
    OAuthRefreshTokenRefresher,
    TokenBundle,
    TokenManager,
    TokenRefreshedHook,
    TokenRefresher,
)
from agentrux.sdk.envelope import ListEventsPage, MessageEnvelope, PublishResult
from agentrux.sdk.errors import (
    AuthorizationPendingError,
    OAuthError,
    SDKError,
    SlowDownError,
)
from agentrux.sdk.hybrid_consumer import HybridConsumer
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.pull_client import PullClient
from agentrux.sdk.sse_client import ResyncFrame

logger = logging.getLogger("agentrux.sdk.facade")


# ---------------------------------------------------------------------------
# Subscription — async-iterable handle returned by AgenTruxClient.subscribe()
# ---------------------------------------------------------------------------


OnResyncRequiredCallback = Callable[[ResyncFrame], Awaitable[None]]


class Subscription:
    """An open consumer on one topic. Supports `async for` and `async with`."""

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        mode: str = "hybrid",
        pipeline: MessagePipeline | None = None,
        start_after_event_id: str | None = None,
        on_resync_required: OnResyncRequiredCallback | None = None,
        checkpoint: CheckpointStore | None = None,
        poll_interval_ms: int = 1000,
        batch_size: int = 100,
    ) -> None:
        if mode not in ("hybrid", "sse", "pull"):
            raise ValueError(
                f"mode must be 'hybrid' / 'sse' / 'pull', got {mode!r}"
            )
        self._api = api_client
        self._topic_id = topic_id
        self._mode = mode
        self._pipeline = pipeline or MessagePipeline()
        self._pipeline.set_topic_id(topic_id)
        self._checkpoint = checkpoint

        # 'sse' alone makes no sense (hints have no body), so it
        # actually maps to a hybrid with the same hint-then-pull path.
        # We keep 'sse' as a config alias for callers who think in those
        # terms.
        sse_enabled = mode in ("hybrid", "sse")

        if mode == "pull":
            self._consumer: PullClient | HybridConsumer = PullClient(
                api_client=api_client,
                topic_id=topic_id,
                poll_interval_ms=poll_interval_ms,
                batch_size=batch_size,
                pipeline=self._pipeline,
                start_after_event_id=start_after_event_id,
            )
        else:
            self._consumer = HybridConsumer(
                api_client=api_client,
                topic_id=topic_id,
                pipeline=self._pipeline,
                start_after_event_id=start_after_event_id,
                poll_interval_ms=poll_interval_ms,
                batch_size=batch_size,
                sse_enabled=sse_enabled,
                on_resync_required=on_resync_required,
            )

    @property
    def mode(self) -> str:
        return self._mode

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.unsubscribe()

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        async for msg in self._consumer:
            yield msg
            # At-least-once: persist checkpoint AFTER yield completes
            # successfully (Python's async-generator protocol resumes
            # here only when the user-loop body returns).
            if self._checkpoint is not None:
                await self._checkpoint.save(
                    self._topic_id, msg.sequence_number, msg.event_id,
                )

    async def unsubscribe(self) -> None:
        await self._consumer.stop()

    @property
    def stats(self) -> Any:
        return self._consumer.stats


# ---------------------------------------------------------------------------
# AgenTruxClient — the SDK surface
# ---------------------------------------------------------------------------


class AgenTruxClient:
    """Authenticated client for the AgenTrux Topic data plane.

    Construct via one of the `from_*` factories — direct __init__ is for
    advanced callers who already have a TokenManager.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token_manager: TokenManager,
        api_client: AgenTruxAPIClient | None = None,
        on_token_refreshed: TokenRefreshedHook | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        if on_token_refreshed is not None and token_manager._on_refreshed is None:  # noqa: SLF001
            token_manager._on_refreshed = on_token_refreshed  # noqa: SLF001
        self._api = api_client or AgenTruxAPIClient(
            base_url=self._base_url,
            token_manager=token_manager,
        )
        self._subscriptions: list[Subscription] = []
        self._closed = False

    # --- Convenience accessors --------------------------------------------

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def access_token(self) -> str:
        return self._token_manager.access_token

    @property
    def refresh_token(self) -> str | None:
        return self._token_manager.refresh_token

    def token_bundle(self) -> TokenBundle:
        return self._token_manager.bundle()

    # --- Lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> AgenTruxClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for sub in list(self._subscriptions):
            try:
                await sub.unsubscribe()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.warning("Error unsubscribing during close", exc_info=True)
        self._subscriptions.clear()
        await self._api.close()

    def _check_open(self) -> None:
        if self._closed:
            raise SDKError("AgenTruxClient is closed")

    # =======================================================================
    # Factories — the SDK's primary entry points
    # =======================================================================

    @classmethod
    async def from_client_credentials(
        cls,
        base_url: str,
        *,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> AgenTruxClient:
        """OAuth 2.1 client_credentials grant for script credentials.

        client_id is the `crd_<uuid>` issued by Console, client_secret
        the `aks_<base64>`. client_credentials does NOT yield a
        refresh_token, so the SDK will simply re-issue when the
        access_token expires.
        """
        api = AgenTruxAPIClient(base_url=base_url)
        try:
            token = await api.oauth_token_client_credentials(
                client_id=client_id,
                client_secret=client_secret,
                scope=scope,
            )
        except Exception:
            await api.close()
            raise
        # client_credentials path: build a refresher that re-issues
        # via the same credentials (since there's no refresh_token).
        refresher = _ClientCredentialsRefresher(
            base_url=base_url,
            client_id=client_id,
            client_secret=client_secret,
            scope=scope,
        )
        tm = TokenManager(
            access_token=token.access_token,
            refresh_token=None,
            refresher=refresher,
            explicit_expires_at_unix=token.expires_at_unix(),
        )
        return cls(base_url=base_url, token_manager=tm, api_client=api)

    @classmethod
    async def from_access_token(
        cls,
        base_url: str,
        *,
        access_token: str,
        refresh_token: str | None = None,
        oauth_client_id: str | None = None,
        client_secret: str | None = None,
        on_token_refreshed: TokenRefreshedHook | None = None,
    ) -> AgenTruxClient:
        """Bring-your-own-token construction.

        If `refresh_token` and `oauth_client_id` are provided, the SDK
        wires up automatic refresh via POST /oauth/token grant_type=
        refresh_token.
        """
        refresher: TokenRefresher | None = None
        if refresh_token and oauth_client_id:
            refresher = OAuthRefreshTokenRefresher(
                base_url=base_url,
                oauth_client_id=oauth_client_id,
                client_secret=client_secret,
            )
        tm = TokenManager(
            access_token=access_token,
            refresh_token=refresh_token,
            refresher=refresher,
            on_refreshed=on_token_refreshed,
        )
        api = AgenTruxAPIClient(base_url=base_url, token_manager=tm)
        return cls(base_url=base_url, token_manager=tm, api_client=api)

    @classmethod
    async def from_activation_code(
        cls,
        base_url: str,
        *,
        activation_code: str,
        save_credentials_to: str | Path | None = None,
    ) -> AgenTruxClient:
        """Redeem an `act_<base64>` code → client_credentials → client.

        Optionally persist the issued (crd_, aks_) pair to disk for
        re-use across process restarts (the code itself is single-use).
        """
        async with AgenTruxAPIClient(base_url=base_url) as api:
            redemption: ActivationCodeRedemption = await api.redeem_activation_code(
                code=activation_code
            )
        if save_credentials_to is not None:
            _save_credentials(
                save_credentials_to,
                {
                    "client_id": redemption.client_id,
                    "client_secret": redemption.client_secret,
                    "script_id": redemption.script_id,
                    "issued_at": redemption.issued_at,
                },
            )
        return await cls.from_client_credentials(
            base_url,
            client_id=redemption.client_id,
            client_secret=redemption.client_secret,
        )

    @classmethod
    async def register_dcr_client(
        cls,
        base_url: str,
        *,
        client_name: str,
        redirect_uris: list[str] | None = None,
        grant_types: list[str] | None = None,
    ) -> DCRRegistration:
        """Register a public OAuth client (RFC 7591) for device flow / MCP."""
        async with AgenTruxAPIClient(base_url=base_url) as api:
            return await api.register_dcr(
                client_name=client_name,
                redirect_uris=redirect_uris,
                grant_types=grant_types,
            )

    @classmethod
    async def start_device_flow(
        cls,
        base_url: str,
        *,
        oauth_client_id: str,
        scope: str | None = None,
    ) -> DeviceAuthorization:
        """Begin RFC 8628 device authorization; returns the codes to present."""
        async with AgenTruxAPIClient(base_url=base_url) as api:
            return await api.device_authorization(
                client_id=oauth_client_id, scope=scope
            )

    @classmethod
    async def complete_device_flow(
        cls,
        base_url: str,
        *,
        device_code: str,
        oauth_client_id: str,
        poll_interval: int = 5,
        max_poll_seconds: int = 600,
    ) -> AgenTruxClient:
        """Poll POST /oauth/token until the user approves or the code expires.

        Implements RFC 8628 §3.5 polling rules: increment interval by 5
        seconds on `slow_down`, stop on `expired_token`, raise on other
        OAuth errors.
        """
        api = AgenTruxAPIClient(base_url=base_url)
        try:
            current_interval = max(1, poll_interval)
            elapsed = 0
            token: OAuthTokenResponse | None = None
            while elapsed < max_poll_seconds:
                try:
                    token = await api.oauth_token_device_code(
                        device_code=device_code,
                        client_id=oauth_client_id,
                    )
                    break
                except SlowDownError:
                    current_interval += 5
                except AuthorizationPendingError:
                    pass
                await asyncio.sleep(current_interval)
                elapsed += current_interval
            if token is None:
                raise SDKError(
                    f"Device flow exhausted poll budget ({max_poll_seconds}s) "
                    "without completion."
                )
        except Exception:
            await api.close()
            raise

        refresher: TokenRefresher | None = None
        if token.refresh_token is not None:
            refresher = OAuthRefreshTokenRefresher(
                base_url=base_url, oauth_client_id=oauth_client_id
            )
        tm = TokenManager(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            refresher=refresher,
            explicit_expires_at_unix=token.expires_at_unix(),
        )
        return cls(base_url=base_url, token_manager=tm, api_client=api)

    # --- Discovery -------------------------------------------------------

    @classmethod
    async def discover(cls, base_url: str) -> AuthorizationServerMetadata:
        """GET /.well-known/oauth-authorization-server (RFC 8414)."""
        async with AgenTruxAPIClient(base_url=base_url) as api:
            return await api.discover_metadata()

    # =======================================================================
    # Data plane (delegates to AgenTruxAPIClient)
    # =======================================================================

    async def publish(
        self,
        topic_id: str,
        event_type: str | None = None,
        payload: Any = None,
        *,
        payload_object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> PublishResult:
        self._check_open()
        return await self._api.publish_event(
            topic_id=topic_id,
            event_type=event_type,
            payload=payload,
            payload_object_id=payload_object_id,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    async def get_event(self, topic_id: str, event_id: str) -> MessageEnvelope:
        self._check_open()
        return await self._api.get_event(topic_id=topic_id, event_id=event_id)

    async def list_events(
        self,
        topic_id: str,
        *,
        after: str | None = None,
        before: str | None = None,
        limit: int = 100,
        order: str = "asc",
        event_type: str | None = None,
        expand: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> ListEventsPage:
        self._check_open()
        return await self._api.list_events(
            topic_id=topic_id,
            after=after,
            before=before,
            limit=limit,
            order=order,
            event_type=event_type,
            expand=expand,
            since=since,
            until=until,
        )

    async def upload_payload(
        self,
        topic_id: str,
        content: bytes,
        *,
        content_type: str | None = None,
    ) -> PayloadUploadTicket:
        """Request a presigned PUT URL and upload `content` to it.

        Returns the ticket (including `payload_object_id`) so the caller
        can attach the object to a subsequent publish() via the
        `payload_object_id=` keyword.
        """
        self._check_open()
        ticket = await self._api.request_payload_upload(
            topic_id=topic_id,
            content_type=content_type,
            size_bytes=len(content),
        )
        await self._api.put_payload_bytes(
            upload_url=ticket.upload_url,
            content=content,
            required_headers=ticket.required_headers,
        )
        return ticket

    async def get_payload(
        self, topic_id: str, payload_object_id: str
    ) -> PayloadDownload:
        """Get a presigned GET URL for an existing payload object."""
        self._check_open()
        return await self._api.get_payload(
            topic_id=topic_id, payload_object_id=payload_object_id
        )

    async def fetch_payload_bytes(self, download: PayloadDownload) -> bytes:
        """Convenience: GET the presigned download URL and return the body."""
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(download.download_url)
            if resp.status_code != 200:
                raise SDKError(
                    f"presigned GET failed: HTTP {resp.status_code} "
                    f"on object {download.payload_object_id}"
                )
            return resp.content

    # =======================================================================
    # Subscribe
    # =======================================================================

    def subscribe(
        self,
        topic_id: str,
        *,
        mode: str = "hybrid",
        start_after_event_id: str | None = None,
        checkpoint: CheckpointStore | None = None,
        on_resync_required: OnResyncRequiredCallback | None = None,
        poll_interval_ms: int = 1000,
        batch_size: int = 100,
    ) -> Subscription:
        """Open a consumer on `topic_id`.

        Returns a `Subscription` that is `async for`-iterable; values are
        `MessageEnvelope`. Save state via `checkpoint` to resume across
        process restarts.
        """
        self._check_open()
        sub = Subscription(
            api_client=self._api,
            topic_id=topic_id,
            mode=mode,
            start_after_event_id=start_after_event_id,
            checkpoint=checkpoint,
            on_resync_required=on_resync_required,
            poll_interval_ms=poll_interval_ms,
            batch_size=batch_size,
        )
        self._subscriptions.append(sub)
        return sub

    async def subscribe_resume(
        self,
        topic_id: str,
        checkpoint: CheckpointStore,
        *,
        mode: str = "hybrid",
        on_resync_required: OnResyncRequiredCallback | None = None,
    ) -> Subscription:
        """Open a subscription resuming from the last checkpointed position.

        If no checkpoint exists, starts from `latest`.
        """
        self._check_open()
        last = await checkpoint.load(topic_id)
        start = last[1] if last is not None else None
        return self.subscribe(
            topic_id,
            mode=mode,
            start_after_event_id=start,
            checkpoint=checkpoint,
            on_resync_required=on_resync_required,
        )

    # =======================================================================
    # Escape hatch — direct access to the low-level client
    # =======================================================================

    @property
    def api(self) -> AgenTruxAPIClient:
        """The underlying low-level HTTP client. Use only when the
        public surface is insufficient (and consider opening an issue)."""
        self._check_open()
        return self._api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ClientCredentialsRefresher:
    """Refresher used when there is no refresh_token (client_credentials).

    Re-runs the original client_credentials exchange to mint a fresh
    access_token. Holds onto the secret in memory only.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope

    async def refresh(self, current_refresh_token: str) -> TokenBundle:  # noqa: ARG002 - signature contract
        import time as _time
        async with AgenTruxAPIClient(base_url=self._base_url) as api:
            token = await api.oauth_token_client_credentials(
                client_id=self._client_id,
                client_secret=self._client_secret,
                scope=self._scope,
            )
        return TokenBundle(
            access_token=token.access_token,
            refresh_token=None,
            expires_at_unix=int(_time.time()) + token.expires_in,
        )


def _save_credentials(path: str | Path, creds: dict[str, str]) -> None:
    """Write credentials JSON with restricted permissions (0600)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Credentials saved to %s (mode 0600)", p)


def _load_credentials(path: str | Path) -> dict[str, str] | None:
    """Read credentials JSON or return None if absent."""
    p = Path(path)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Convenience top-level functions
# ---------------------------------------------------------------------------


async def connect(
    base_url: str,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    oauth_client_id: str | None = None,
    client_id: str | None = None,
    client_secret: str | None = None,
    activation_code: str | None = None,
    on_token_refreshed: TokenRefreshedHook | None = None,
) -> AgenTruxClient:
    """One-stop factory — picks the right `from_*` based on what's given.

    Priority: explicit access_token > client_credentials > activation_code.
    Pass exactly one credential source.
    """
    sources = sum(
        1
        for x in (access_token, client_id, activation_code)
        if x is not None
    )
    if sources != 1:
        raise ValueError(
            "connect() requires exactly one of access_token, client_id, "
            "or activation_code"
        )
    if access_token is not None:
        return await AgenTruxClient.from_access_token(
            base_url,
            access_token=access_token,
            refresh_token=refresh_token,
            oauth_client_id=oauth_client_id,
            client_secret=client_secret,
            on_token_refreshed=on_token_refreshed,
        )
    if client_id is not None:
        if client_secret is None:
            raise ValueError(
                "client_secret is required when client_id is supplied"
            )
        return await AgenTruxClient.from_client_credentials(
            base_url, client_id=client_id, client_secret=client_secret
        )
    # activation_code branch
    return await AgenTruxClient.from_activation_code(
        base_url, activation_code=activation_code  # type: ignore[arg-type]
    )


__all__ = [
    "AgenTruxClient",
    "Subscription",
    "connect",
    "OnResyncRequiredCallback",
]
_ = OAuthError  # re-export marker (intentionally referenced)
