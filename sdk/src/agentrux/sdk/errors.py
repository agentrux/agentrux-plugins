"""AgenTrux SDK exception hierarchy.

Maps 1:1 to AgenTrux server's two error envelope systems:

  1. FastAPI HTTPException envelope (pipe / console / admin):
        {"detail": {"error": "<CODE>", "message": "...", "details": {...}?,
                    "next_action": "..."?}}
     Codes: INVALID / UNAUTHORIZED / FORBIDDEN / SUSPENDED / NOT_FOUND /
            CONFLICT / RATE_LIMITED / PAYLOAD_TOO_LARGE / SERVICE_UNAVAILABLE /
            INTERNAL  (error_model.md §3 closed set, 10 codes)

  2. RFC 6749 / RFC 8628 OAuth envelope (oauth_*):
        {"error": "<RFC code>", "error_description": "..."}
     Codes: invalid_request / invalid_client / invalid_grant / unauthorized_client
            / unsupported_grant_type / invalid_scope / authorization_pending /
            slow_down / access_denied / expired_token

SDK callers handle the two hierarchies separately (see APIError vs OAuthError).
"""
from __future__ import annotations

from typing import Any


class SDKError(Exception):
    """Base class for all SDK errors."""


# ---------- Local (no HTTP) errors ----------


class ConfigurationError(SDKError):
    """SDK was misconfigured locally (missing creds, bad URL, etc.)."""


class CredentialPersistenceError(SDKError):
    """Failed to read/write the credentials file on disk."""


class CheckpointLockedError(SDKError):
    """Another process holds the checkpoint file lock."""


class CheckpointOrderError(SDKError):
    """Attempted to save a sequence_number smaller than the last saved one."""


# ---------- API error (pipe / console FastAPI envelope) ----------


class APIError(SDKError):
    """Base class for server-returned errors from the data plane / console.

    Carries the server's structured `detail` payload so callers can
    inspect `code` ("INVALID", "RATE_LIMITED", etc.), `next_action`
    ("retry_after", "cursor_advance"...) and `details` dict for retry
    decisions.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        details: dict[str, Any] | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}
        self.next_action = next_action

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status={self.status_code}, "
            f"code={self.code!r}, message={self.args[0]!r}, "
            f"next_action={self.next_action!r})"
        )


class InvalidRequestError(APIError):
    """422 INVALID — request payload / params failed validation."""


class UnauthorizedError(APIError):
    """401 UNAUTHORIZED — missing or invalid bearer token."""


class ForbiddenError(APIError):
    """403 FORBIDDEN — token valid but scope missing / topic suspended."""


class SuspendedError(ForbiddenError):
    """403 SUSPENDED — caller account or target topic is suspended."""


class NotFoundError(APIError):
    """404 NOT_FOUND — resource missing or hidden by A4 cross-tenant rule."""


class TTLExpiredError(NotFoundError):
    """404 NOT_FOUND with details.reason='ttl_expired' — cursor past retention.

    `next_action` is always 'cursor_advance'. The caller's gap_detector
    should use `details.oldest_available_evt_id` to resync.
    """

    @property
    def oldest_available_evt_id(self) -> str | None:
        return self.details.get("oldest_available_evt_id")


class ConflictError(APIError):
    """409 CONFLICT — idempotency / state-conditional violation."""


class IdempotencyConflictError(ConflictError):
    """409 CONFLICT on an Idempotency-Key replay with non-matching fingerprint."""


class RateLimitedError(APIError):
    """429 RATE_LIMITED — server-side throttling triggered.

    `next_action` is 'retry_after'; `details.retry_after_seconds` gives
    the cooldown. Also reflected in the `Retry-After` HTTP header.
    """

    @property
    def retry_after_seconds(self) -> int | None:
        value = self.details.get("retry_after_seconds")
        return int(value) if value is not None else None


class PayloadTooLargeError(APIError):
    """413 PAYLOAD_TOO_LARGE — inline payload above 256 KiB cap."""


class ServiceUnavailableError(APIError):
    """503 SERVICE_UNAVAILABLE — object storage or downstream temporarily down."""


class InternalServerError(APIError):
    """500 INTERNAL — server bug, not the client's fault."""


# Pipe error code → exception class registry. Used by the client's HTTP
# layer to construct the right subclass after parsing the envelope.
_PIPE_CODE_REGISTRY: dict[str, type[APIError]] = {
    "INVALID": InvalidRequestError,
    "UNAUTHORIZED": UnauthorizedError,
    "FORBIDDEN": ForbiddenError,
    "SUSPENDED": SuspendedError,
    "NOT_FOUND": NotFoundError,
    "CONFLICT": ConflictError,
    "RATE_LIMITED": RateLimitedError,
    "PAYLOAD_TOO_LARGE": PayloadTooLargeError,
    "SERVICE_UNAVAILABLE": ServiceUnavailableError,
    "INTERNAL": InternalServerError,
}


def api_error_from_detail(
    *, status_code: int, detail: dict[str, Any]
) -> APIError:
    """Build the right APIError subclass from a FastAPI `detail` dict.

    `detail` shape (pipe_router.py: _invalid / _forbidden / _not_found /
    _conflict / _ttl_expired_*):
        {"error": "<CODE>", "message": "...", "details": {...}?, "next_action": "..."?}
    """
    code = str(detail.get("error", "INTERNAL"))
    message = str(detail.get("message", "<no message>"))
    details = detail.get("details") if isinstance(detail.get("details"), dict) else None
    next_action = detail.get("next_action")

    cls = _PIPE_CODE_REGISTRY.get(code, APIError)

    # Specialize NOT_FOUND with reason=ttl_expired to TTLExpiredError.
    if (
        cls is NotFoundError
        and isinstance(details, dict)
        and details.get("reason") == "ttl_expired"
    ):
        cls = TTLExpiredError
    # Specialize CONFLICT for Idempotency-Key fingerprint mismatch.
    if (
        cls is ConflictError
        and isinstance(details, dict)
        and details.get("reason") == "idempotency_fingerprint_mismatch"
    ):
        cls = IdempotencyConflictError

    return cls(
        message,
        status_code=status_code,
        code=code,
        details=details,
        next_action=next_action,
    )


# ---------- OAuth error (RFC 6749 §5.2 + RFC 8628 §3.5 envelope) ----------


class OAuthError(SDKError):
    """Base for OAuth token / DCR / device-flow errors.

    The OAuth flat envelope is `{"error": "<RFC code>", "error_description": "..."}`,
    plus optional `error_uri`. The HTTP status is 400 / 401 for most errors
    and 403 for access_denied.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error: str,
        error_description: str | None = None,
        error_uri: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.error_description = error_description
        self.error_uri = error_uri


class InvalidClientError(OAuthError):
    """invalid_client — bad client_id or client_secret."""


class InvalidGrantError(OAuthError):
    """invalid_grant — expired/invalid code, refresh_token, or device_code."""


class UnsupportedGrantTypeError(OAuthError):
    """unsupported_grant_type — server doesn't support that grant_type."""


class AuthorizationPendingError(OAuthError):
    """authorization_pending (RFC 8628) — keep polling at the same interval."""


class SlowDownError(OAuthError):
    """slow_down (RFC 8628) — server asks to back off polling by +5 seconds."""


class AccessDeniedError(OAuthError):
    """access_denied — user denied the consent."""


class ExpiredTokenError(OAuthError):
    """expired_token (RFC 8628) — device_code TTL passed before user consented."""


_OAUTH_CODE_REGISTRY: dict[str, type[OAuthError]] = {
    "invalid_client": InvalidClientError,
    "invalid_grant": InvalidGrantError,
    "unsupported_grant_type": UnsupportedGrantTypeError,
    "authorization_pending": AuthorizationPendingError,
    "slow_down": SlowDownError,
    "access_denied": AccessDeniedError,
    "expired_token": ExpiredTokenError,
}


def oauth_error_from_body(
    *, status_code: int, body: dict[str, Any]
) -> OAuthError:
    """Build the right OAuthError subclass from RFC 6749 / RFC 8628 body."""
    error = str(body.get("error", "invalid_request"))
    description = body.get("error_description")
    uri = body.get("error_uri")
    cls = _OAUTH_CODE_REGISTRY.get(error, OAuthError)
    return cls(
        description or error,
        status_code=status_code,
        error=error,
        error_description=description,
        error_uri=uri,
    )


# ---------- Stream-specific errors ----------


class ResyncRequiredError(SDKError):
    """SSE `event: resync_required` frame was received.

    Behavior depends on the consumer layer:
      - `SSEClient` (low-level): if `on_resync_required` callback is
        registered, callback runs and the exception is NOT raised; if
        no callback, the exception is raised so the caller learns.
      - `HybridConsumer` (high-level, used by `AgenTruxClient.subscribe`):
        ALWAYS raised after the callback returns. The callback is for
        side-channel notification (log/metric/push) only — the SDK
        decides control flow because a dead cursor must terminate the
        consumer and the application must restart with fresh state.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        request_id: str,
        resume_via: str | None = None,
        max_catchup: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.request_id = request_id
        self.resume_via = resume_via
        self.max_catchup = max_catchup


class ConnectionBannedError(SDKError):
    """SSE `event: close` with reason='banned' was received.

    The account has been suspended; reconnect attempts will 403.
    """


class GapUnrecoverableError(SDKError):
    """gap_detector cannot fill a sequence gap.

    Raised only when the caller did not register an on_unrecoverable
    callback. With server's by-sequence API removed (Phase 1.9+),
    any detected gap is immediately unrecoverable from the SDK's side —
    callers must resync from `latest` or rely on the `ttl_expires_at`
    boundary in the topic-state block.
    """

    def __init__(
        self, message: str, *, gap_start_seq: int, gap_end_seq: int
    ) -> None:
        super().__init__(message)
        self.gap_start_seq = gap_start_seq
        self.gap_end_seq = gap_end_seq
