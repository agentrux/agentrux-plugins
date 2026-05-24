"""AgenTrux Python SDK — v0.3 (OAuth 2.1 + Phase 2.5 cursor + SSE hint-only).

Public entry point:

    from agentrux.sdk import AgenTruxClient

    client = await AgenTruxClient.from_client_credentials(
        "https://api.agentrux.com",
        client_id="crd_<uuid>",
        client_secret="aks_<...>",
    )
    await client.publish("top_<uuid>", "hello.world", {"msg": "hi"})

    async with client.subscribe("top_<uuid>") as sub:
        async for envelope in sub:
            print(envelope.event_type, envelope.payload)

See README.md for the full surface (DCR, device flow, AC redeem,
checkpoint, resync_required handling).
"""
from agentrux.sdk.auth_models import (
    ActivationCodeRedemption,
    AuthorizationServerMetadata,
    DCRRegistration,
    DeviceAuthorization,
    OAuthTokenResponse,
    PayloadDownload,
    PayloadUploadTicket,
)
from agentrux.sdk.checkpoint import (
    CheckpointStats,
    CheckpointStore,
    FileCheckpointStore,
)
from agentrux.sdk.client import (
    AgenTruxAPIClient,
    OAuthRefreshTokenRefresher,
    TokenBundle,
    TokenManager,
    TokenRefresher,
)
from agentrux.sdk.deduplicator import Deduplicator
from agentrux.sdk.envelope import (
    ListEventsPage,
    MessageEnvelope,
    PageCursor,
    PublishResult,
    TopicCursorState,
)
from agentrux.sdk.errors import (
    APIError,
    AccessDeniedError,
    AuthorizationPendingError,
    CheckpointLockedError,
    CheckpointOrderError,
    ConflictError,
    ConnectionBannedError,
    ExpiredTokenError,
    ForbiddenError,
    GapUnrecoverableError,
    IdempotencyConflictError,
    InternalServerError,
    InvalidClientError,
    InvalidGrantError,
    InvalidRequestError,
    NotFoundError,
    OAuthError,
    PayloadTooLargeError,
    RateLimitedError,
    ResyncRequiredError,
    SDKError,
    ServiceUnavailableError,
    SlowDownError,
    SuspendedError,
    TTLExpiredError,
    UnauthorizedError,
    UnsupportedGrantTypeError,
)
from agentrux.sdk.facade import AgenTruxClient, Subscription, connect
from agentrux.sdk.flow_controller import FlowController
from agentrux.sdk.gap_detector import FillResult, GapDetector, GapState
from agentrux.sdk.hybrid_consumer import HybridConsumer
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.pull_client import PullClient
from agentrux.sdk.reconnect import ExponentialBackoff
from agentrux.sdk.reorder_buffer import ReorderBuffer
from agentrux.sdk.sse_client import HintFrame, ReadyFrame, ResyncFrame, SSEClient
from agentrux.sdk.stats import SDKStats


__all__ = [
    # Top-level
    "AgenTruxClient",
    "Subscription",
    "connect",
    # Auth dataclasses
    "OAuthTokenResponse",
    "DCRRegistration",
    "DeviceAuthorization",
    "ActivationCodeRedemption",
    "AuthorizationServerMetadata",
    "PayloadUploadTicket",
    "PayloadDownload",
    # Event dataclasses
    "MessageEnvelope",
    "PublishResult",
    "ListEventsPage",
    "PageCursor",
    "TopicCursorState",
    # Low-level
    "AgenTruxAPIClient",
    "TokenManager",
    "TokenBundle",
    "TokenRefresher",
    "OAuthRefreshTokenRefresher",
    # Consumers
    "PullClient",
    "SSEClient",
    "HybridConsumer",
    "HintFrame",
    "ReadyFrame",
    "ResyncFrame",
    # Pipeline primitives
    "MessagePipeline",
    "Deduplicator",
    "ReorderBuffer",
    "FlowController",
    "GapDetector",
    "GapState",
    "FillResult",
    "ExponentialBackoff",
    "CheckpointStore",
    "FileCheckpointStore",
    "CheckpointStats",
    "SDKStats",
    # Errors
    "SDKError",
    "APIError",
    "InvalidRequestError",
    "UnauthorizedError",
    "ForbiddenError",
    "SuspendedError",
    "NotFoundError",
    "TTLExpiredError",
    "ConflictError",
    "IdempotencyConflictError",
    "RateLimitedError",
    "PayloadTooLargeError",
    "ServiceUnavailableError",
    "InternalServerError",
    "OAuthError",
    "InvalidClientError",
    "InvalidGrantError",
    "UnsupportedGrantTypeError",
    "AuthorizationPendingError",
    "SlowDownError",
    "AccessDeniedError",
    "ExpiredTokenError",
    "ResyncRequiredError",
    "ConnectionBannedError",
    "GapUnrecoverableError",
    "CheckpointLockedError",
    "CheckpointOrderError",
]
