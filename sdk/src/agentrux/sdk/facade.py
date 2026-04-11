"""High-level facade for AgenTrux SDK.

Provides AgenTruxClient, Subscription, and connect() for simple A2A usage.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from typing import Any, AsyncIterator, Awaitable, Callable

from agentrux.sdk.checkpoint import CheckpointStore
from agentrux.sdk.client import AgenTruxAPIClient, TokenRefresher
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.errors import SDKError
from agentrux.sdk.gap_detector import GapDetector
from agentrux.sdk.hybrid_consumer import HybridConsumer
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.pull_client import PullClient
from agentrux.sdk.sse_client import SSEClient
from agentrux.sdk.stats import SDKStats

UnrecoverableCallback = Callable[[int, int, str], Awaitable[None]]

logger = logging.getLogger("agentrux.sdk.facade")


class Subscription:
    """Subscribe handle. Supports async for and async with."""

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        mode: str,
        pipeline: MessagePipeline,
        checkpoint: CheckpointStore | None = None,
        start_sequence: int | None = None,
    ):
        self._api = api_client
        self._topic_id = topic_id
        self._mode = mode
        self._pipeline = pipeline
        self._checkpoint = checkpoint
        self._start_sequence = start_sequence
        self._consumer: SSEClient | PullClient | HybridConsumer | None = None
        self._build_consumer()

    def _build_consumer(self) -> None:
        if self._mode == "hybrid":
            self._consumer = HybridConsumer(
                api_client=self._api,
                topic_id=self._topic_id,
                pipeline=self._pipeline,
                start_sequence=self._start_sequence,
            )
        elif self._mode == "sse":
            self._consumer = SSEClient(
                api_client=self._api,
                topic_id=self._topic_id,
                pipeline=self._pipeline,
                start_sequence=self._start_sequence,
            )
        elif self._mode == "pull":
            self._consumer = PullClient(
                api_client=self._api,
                topic_id=self._topic_id,
                pipeline=self._pipeline,
                start_sequence=self._start_sequence,
            )
        else:
            raise ValueError(f"Invalid mode: {self._mode!r}. Use 'hybrid', 'sse', or 'pull'.")

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.unsubscribe()

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        """Iterate messages in strict sequence_no order.

        If a checkpoint is configured, save() is called AFTER yielding
        each message — but Python's async generator protocol means save()
        actually runs after the consumer's next() call, which is on the
        same iteration of the user's `async for` loop, AFTER the user
        body has finished. This preserves at-least-once: if the user
        body raises, save() is never reached for that seq.
        """
        if self._consumer is None:
            raise SDKError("Subscription already unsubscribed")
        async for msg in self._consumer:
            yield msg
            # Reaches here only if user body completed without raising
            if self._checkpoint is not None:
                await self._checkpoint.save(
                    self._topic_id, msg.sequence_no, msg.event_id,
                )

    async def unsubscribe(self) -> None:
        """Stop subscription and release resources."""
        if self._consumer is None:
            return
        if isinstance(self._consumer, SSEClient):
            await self._consumer.disconnect()
        elif isinstance(self._consumer, (HybridConsumer, PullClient)):
            await self._consumer.stop()
        self._consumer = None

    @property
    def stats(self) -> SDKStats:
        if self._consumer is None:
            return SDKStats(current_mode="disconnected")
        return self._consumer.stats

    @property
    def mode(self) -> str:
        if self._consumer is None:
            return "disconnected"
        if isinstance(self._consumer, HybridConsumer):
            return self._consumer.mode
        return self._mode


class AgenTruxClient:
    """High-level AgenTrux client.

    Simple interface for LLM and A2A scripts.
    Internally composes AgenTruxAPIClient + MessagePipeline + consumers.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        refresh_token: str | None = None,
        token_refresher: TokenRefresher | None = None,
        timeout_s: float = 30.0,
    ):
        if not base_url:
            raise ValueError("base_url must not be empty")

        self._base_url = base_url
        self._api = AgenTruxAPIClient(
            base_url=base_url,
            token=token,
            refresh_token=refresh_token,
            token_refresher=token_refresher,
            timeout_s=timeout_s,
        )
        self._subscriptions: list[Subscription] = []
        self._closed = False

    async def __aenter__(self) -> AgenTruxClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Release all resources. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True

        for sub in self._subscriptions:
            try:
                await sub.unsubscribe()
            except Exception:
                logger.warning("Error unsubscribing during close", exc_info=True)
        self._subscriptions.clear()

        await self._api.close()

    def _check_closed(self) -> None:
        if self._closed:
            raise SDKError("Client is closed")

    # --- Publish ---

    async def publish(
        self,
        topic_id: str,
        event_type: str,
        payload: dict | None = None,
        *,
        payload_ref: str | None = None,
    ) -> str:
        """Publish an event. Returns event_id."""
        self._check_closed()
        return await self._api.publish_event(
            topic_id=topic_id,
            type=event_type,
            payload=payload,
            payload_ref=payload_ref,
        )

    # --- Subscribe ---

    def subscribe(
        self,
        topic_id: str,
        *,
        mode: str = "hybrid",
        start: str = "latest",
        start_sequence: int | None = None,
        gap_fill: bool = True,
        on_gap_unrecoverable: UnrecoverableCallback | None = None,
        checkpoint: CheckpointStore | None = None,
    ) -> Subscription:
        """Subscribe to a topic. Returns an async-iterable Subscription.

        Args:
            topic_id: target topic UUID
            mode: 'hybrid' | 'sse' | 'pull'
            start: 'latest' | 'earliest' | 'sequence'
            start_sequence: required when start='sequence'
            gap_fill: enable GapDetector for automatic backfill of missed seqs
                via the by-sequence REST API. Default True.
            on_gap_unrecoverable: callback fired when a gap cannot be filled
                (retention boundary or events deleted by cleanup job).
                Signature: async (start_seq, end_seq, reason) -> None.
                Reason is currently always 'unrecoverable'.
        """
        self._check_closed()

        valid_modes = ("hybrid", "sse", "pull")
        if mode not in valid_modes:
            raise ValueError(f"Invalid mode: {mode!r}. Use one of {valid_modes}")

        valid_starts = ("latest", "earliest", "sequence")
        if start not in valid_starts:
            raise ValueError(f"Invalid start: {start!r}. Use one of {valid_starts}")

        if start == "sequence" and start_sequence is None:
            raise ValueError("start_sequence is required when start='sequence'")

        gap_detector = None
        if gap_fill:
            gap_detector = GapDetector(
                api_client=self._api,
                on_unrecoverable=on_gap_unrecoverable,
            )

        pipeline = MessagePipeline(gap_detector=gap_detector)
        pipeline.set_topic_id(topic_id)

        if start == "sequence" and start_sequence is not None:
            pipeline.set_initial_sequence(start_sequence)

        sub = Subscription(
            api_client=self._api,
            topic_id=topic_id,
            mode=mode,
            pipeline=pipeline,
            checkpoint=checkpoint,
            start_sequence=start_sequence if start == "sequence" else None,
        )
        self._subscriptions.append(sub)
        return sub

    async def subscribe_resume(
        self,
        topic_id: str,
        checkpoint: CheckpointStore,
        *,
        mode: str = "hybrid",
        gap_fill: bool = True,
        on_gap_unrecoverable: UnrecoverableCallback | None = None,
    ) -> Subscription:
        """Subscribe and resume from the checkpointed position.

        If a checkpoint exists for the topic, the subscription starts from
        last_seq + 1 and the pipeline's GapDetector backfills any events
        between last_seq + 1 and the current head. SSE consumers also
        send Last-Event-ID = last_seq so server-side replay aligns; the
        Deduplicator absorbs any overlap.

        If no checkpoint exists, this behaves like subscribe(start='latest').
        """
        self._check_closed()
        last = await checkpoint.load(topic_id)
        if last is None:
            return self.subscribe(
                topic_id,
                mode=mode,
                gap_fill=gap_fill,
                on_gap_unrecoverable=on_gap_unrecoverable,
                checkpoint=checkpoint,
            )

        last_seq, _ = last
        return self.subscribe(
            topic_id,
            mode=mode,
            start="sequence",
            start_sequence=last_seq + 1,
            gap_fill=gap_fill,
            on_gap_unrecoverable=on_gap_unrecoverable,
            checkpoint=checkpoint,
        )

    # --- Auth / Provisioning ---

    async def activate(self, activation_code: str) -> dict:
        """Activate script. POST /auth/activate."""
        return await AgenTruxAPIClient.auth_request(
            self._base_url, "/auth/activate", {"activation_code": activation_code},
        )

    async def redeem_grant(self, invite_code: str, script_id: str, client_secret: str) -> dict:
        """Redeem an invite code. POST /auth/redeem-invite-code."""
        return await AgenTruxAPIClient.auth_request(
            self._base_url, "/auth/redeem-invite-code",
            {"invite_code": invite_code, "script_id": script_id, "client_secret": client_secret},
        )

    async def get_token(self, script_id: str, client_secret: str) -> dict:
        """Get JWT. POST /auth/token."""
        return await AgenTruxAPIClient.auth_request(
            self._base_url, "/auth/token",
            {"script_id": script_id, "client_secret": client_secret},
        )

    @staticmethod
    async def bootstrap(
        base_url: str,
        *,
        activation_code: str | None = None,
        invite_code: str | None = None,
        credentials_file: str = ".agentrux_credentials.json",
    ) -> AgenTruxClient:
        """Auto-provision and return an authenticated client.

        First run: activate -> redeem grant -> get token -> save credentials.
        Subsequent runs: load credentials -> get token.

        The credentials_file contains sensitive data (script_id, client_secret).
        Do NOT commit it to version control. File permissions are set to 0600.

        Args:
            base_url: AgenTrux server URL
            activation_code: Activation code (first run only)
            invite_code: Invite code (cross-account only)
            credentials_file: Path to save/load credentials
        """
        creds = _load_credentials(credentials_file)

        # First-time activation
        if "script_id" not in creds and activation_code:
            logger.info("Activating script with activation code...")
            result = await AgenTruxAPIClient.auth_request(
                base_url, "/auth/activate", {"activation_code": activation_code},
            )
            creds["script_id"] = result["script_id"]
            creds["client_secret"] = result["client_secret"]
            _save_credentials(credentials_file, creds)

        if "script_id" not in creds:
            raise SDKError(
                "No credentials found. Provide activation_code for first-time setup, "
                f"or ensure {credentials_file} exists."
            )

        script_id = creds["script_id"]
        client_secret = creds["client_secret"]

        # Redeem invite code if provided
        if invite_code:
            logger.info("Redeeming invite code...")
            await AgenTruxAPIClient.auth_request(
                base_url, "/auth/redeem-invite-code",
                {"invite_code": invite_code, "script_id": script_id, "client_secret": client_secret},
            )
            logger.info("Grant redeemed successfully")

        # Get JWT (with automatic Client secret rotation on expiry)
        logger.info("Obtaining JWT...")
        try:
            token_data = await AgenTruxAPIClient.auth_request(
                base_url, "/auth/token",
                {"script_id": script_id, "client_secret": client_secret},
            )
        except Exception as e:
            err_str = str(e).lower()
            if "expired" in err_str or "client_secret" in err_str or "key" in err_str:
                # Auto-rotate: call /auth/rotate-key with current credentials
                logger.warning("Client secret expired, attempting auto-rotation...")
                try:
                    rotate_data = await AgenTruxAPIClient.auth_request(
                        base_url, "/auth/rotate-key",
                        {"script_id": script_id, "client_secret": client_secret},
                    )
                    # Save new credentials
                    creds["client_secret"] = rotate_data["client_secret"]
                    _save_credentials(credentials_file, creds)
                    logger.info("Client secret auto-rotated and saved to %s", credentials_file)
                    token_data = {
                        "access_token": rotate_data["access_token"],
                        "refresh_token": rotate_data.get("refresh_token"),
                    }
                except Exception as rotate_err:
                    from agentrux.sdk.errors import ClientSecretExpiredError
                    raise ClientSecretExpiredError(
                        "Script Client secret has expired and auto-rotation failed. "
                        "Ask your administrator to rotate the key via the dashboard "
                        "(Scripts → your script → Rotate Key)."
                    ) from rotate_err
            else:
                raise

        return AgenTruxClient(
            base_url=base_url,
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
        )

    # --- Utility ---

    async def get_event(self, topic_id: str, event_id: str) -> MessageEnvelope:
        """Get a single event."""
        self._check_closed()
        data = await self._api.get_event(topic_id, event_id)
        return MessageEnvelope.from_api_response(data)

    async def list_events(
        self,
        topic_id: str,
        *,
        limit: int = 50,
        cursor: str | None = None,
        event_type: str | None = None,
    ) -> tuple[list[MessageEnvelope], str | None]:
        """List events. Returns (events, next_cursor)."""
        self._check_closed()
        items, next_cursor = await self._api.list_events(
            topic_id=topic_id,
            cursor=cursor,
            limit=limit,
            event_type=event_type,
        )
        envelopes = [MessageEnvelope.from_api_response(item) for item in items]
        return envelopes, next_cursor

    @property
    def api(self) -> AgenTruxAPIClient:
        """Access the low-level API client."""
        self._check_closed()
        return self._api


def connect(
    base_url: str,
    token: str,
    *,
    refresh_token: str | None = None,
    token_refresher: TokenRefresher | None = None,
    timeout_s: float = 30.0,
) -> AgenTruxClient:
    """Create an AgenTruxClient. Use with async with.

    Example:
        async with agentrux.connect("https://...", token) as client:
            await client.publish(topic_id, "event.type", {"key": "value"})
    """
    return AgenTruxClient(
        base_url=base_url,
        token=token,
        refresh_token=refresh_token,
        token_refresher=token_refresher,
        timeout_s=timeout_s,
    )


# --- Credential file helpers (extracted from bootstrap) ---


def _load_credentials(path: str) -> dict[str, str]:
    """Load credentials from JSON file, or return empty dict if not found."""
    if os.path.exists(path):
        with open(path) as f:
            creds = json.load(f)
        logger.info("Loaded credentials from %s", path)
        return creds
    return {}


def _save_credentials(path: str, creds: dict[str, str]) -> None:
    """Save credentials to JSON file with restricted permissions (0600)."""
    with open(path, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Credentials saved to %s", path)
