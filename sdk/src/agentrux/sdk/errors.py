"""SDK 例外階層.

SSOT: docs/04_design/sdk/sdk_design.md §7

すべての SDK 例外は AgenTruxError を継承する。
caller は `except AgenTruxError` で全エラーを捕捉可能。
"""

from __future__ import annotations


class AgenTruxError(Exception):
    """SDK のすべての例外の base class."""


class ConfigError(AgenTruxError):
    """無効な endpoint / 必須 credentials 不足 (起動時)."""


class AuthenticationError(AgenTruxError):
    """401 invalid_token が 2 回連続 (1 回目自動再 issue 後も失敗)."""


class CredentialRotatedError(AgenTruxError):
    """401 invalid_client: client_secret が rotate/revoke された."""


class PermissionDeniedError(AgenTruxError):
    """403 scope_mismatch / FORBIDDEN."""


class ResourceNotFoundError(AgenTruxError):
    """404 (topic / event / payload など)."""


class ConflictError(AgenTruxError):
    """409 (resource conflict、 idempotency body mismatch 等)."""


class IdempotencyConflictError(ConflictError):
    """409 idempotency_conflict: 同 key で異 body の retry."""


class PayloadTooLargeError(AgenTruxError):
    """413: object_ref 自動切替で通常到達しない。 手動経路の安全網."""


class RateLimitError(AgenTruxError):
    """429: retry_after 属性で再試行までの秒数を提供."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ValidationError(AgenTruxError):
    """client-side validation (schema 違反、 size 計算、 prefix 不正等)."""


class TemporaryError(AgenTruxError):
    """5xx / 503 (retry 推奨). exponential backoff の対象."""


class ServerError(TemporaryError):
    """5xx generic."""


class ObjectStorageError(TemporaryError):
    """503 object_storage_error (presigned PUT / GET 失敗)."""


class GapDetectedError(AgenTruxError):
    """pipeline 専用: sequence gap を検出 (re-replay は ops 判断)."""

    def __init__(self, message: str, *, topic_id: str, gap_after: int, gap_size: int) -> None:
        super().__init__(message)
        self.topic_id = topic_id
        self.gap_after = gap_after
        self.gap_size = gap_size
