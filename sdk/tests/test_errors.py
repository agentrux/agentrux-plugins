"""Tests for errors.py: envelope parsers + specialization routing."""
from __future__ import annotations

import pytest

from agentrux.sdk.errors import (
    APIError,
    AccessDeniedError,
    AuthorizationPendingError,
    ConflictError,
    ExpiredTokenError,
    ForbiddenError,
    IdempotencyConflictError,
    InternalServerError,
    InvalidClientError,
    InvalidGrantError,
    InvalidRequestError,
    NotFoundError,
    OAuthError,
    PayloadTooLargeError,
    RateLimitedError,
    ServiceUnavailableError,
    SlowDownError,
    SuspendedError,
    TTLExpiredError,
    UnauthorizedError,
    UnsupportedGrantTypeError,
    api_error_from_detail,
    oauth_error_from_body,
)


# ---------- pipe envelope → APIError subclass routing ------------------


@pytest.mark.parametrize(
    "code,expected_cls",
    [
        ("INVALID", InvalidRequestError),
        ("UNAUTHORIZED", UnauthorizedError),
        ("FORBIDDEN", ForbiddenError),
        ("SUSPENDED", SuspendedError),
        ("NOT_FOUND", NotFoundError),
        ("CONFLICT", ConflictError),
        ("RATE_LIMITED", RateLimitedError),
        ("PAYLOAD_TOO_LARGE", PayloadTooLargeError),
        ("SERVICE_UNAVAILABLE", ServiceUnavailableError),
        ("INTERNAL", InternalServerError),
    ],
)
def test_api_error_from_detail_routes_code_to_subclass(
    code: str, expected_cls: type
) -> None:
    exc = api_error_from_detail(
        status_code=400 if code == "INVALID" else 500,
        detail={"error": code, "message": f"{code} happened"},
    )
    assert isinstance(exc, expected_cls)
    assert exc.code == code
    assert exc.message_field() if hasattr(exc, "message_field") else str(exc) == f"{code} happened"  # noqa


def test_api_error_unknown_code_falls_back_to_apierror() -> None:
    exc = api_error_from_detail(
        status_code=418, detail={"error": "I_AM_A_TEAPOT", "message": "?"}
    )
    assert type(exc) is APIError  # not a subclass
    assert exc.code == "I_AM_A_TEAPOT"


def test_api_error_ttl_expired_specialization() -> None:
    exc = api_error_from_detail(
        status_code=404,
        detail={
            "error": "NOT_FOUND",
            "message": "expired",
            "details": {
                "reason": "ttl_expired",
                "oldest_available_evt_id": "evt_xyz",
            },
            "next_action": "cursor_advance",
        },
    )
    assert isinstance(exc, TTLExpiredError)
    assert exc.oldest_available_evt_id == "evt_xyz"
    assert exc.next_action == "cursor_advance"


def test_api_error_not_found_without_reason_stays_not_found() -> None:
    exc = api_error_from_detail(
        status_code=404, detail={"error": "NOT_FOUND", "message": "x"}
    )
    assert type(exc) is NotFoundError
    assert not isinstance(exc, TTLExpiredError)


def test_api_error_idempotency_conflict_specialization() -> None:
    exc = api_error_from_detail(
        status_code=409,
        detail={
            "error": "CONFLICT",
            "message": "fingerprint mismatch",
            "details": {"reason": "idempotency_fingerprint_mismatch"},
        },
    )
    assert isinstance(exc, IdempotencyConflictError)


def test_api_error_conflict_without_idempotency_reason_stays_conflict() -> None:
    exc = api_error_from_detail(
        status_code=409, detail={"error": "CONFLICT", "message": "x"}
    )
    assert type(exc) is ConflictError


def test_api_error_rate_limited_retry_after_seconds_property() -> None:
    exc = api_error_from_detail(
        status_code=429,
        detail={
            "error": "RATE_LIMITED",
            "message": "throttled",
            "details": {"retry_after_seconds": 30, "scope": "global"},
            "next_action": "retry_after",
        },
    )
    assert isinstance(exc, RateLimitedError)
    assert exc.retry_after_seconds == 30


def test_api_error_rate_limited_no_retry_after_returns_none() -> None:
    exc = api_error_from_detail(
        status_code=429, detail={"error": "RATE_LIMITED", "message": "x"}
    )
    assert exc.retry_after_seconds is None


def test_api_error_details_must_be_dict_or_dropped() -> None:
    # Server should always emit dict; but if it emits a list (Pydantic
    # 422 case), parser shouldn't crash — details just becomes None.
    exc = api_error_from_detail(
        status_code=422,
        detail={"error": "INVALID", "message": "x", "details": ["loc"]},
    )
    # details is dropped to None when not a dict (defensive)
    assert exc.details == {}


def test_api_error_default_code_when_missing_is_internal() -> None:
    exc = api_error_from_detail(status_code=500, detail={"message": "boom"})
    assert exc.code == "INTERNAL"


# ---------- OAuth envelope → OAuthError subclass routing ---------------


@pytest.mark.parametrize(
    "rfc_code,expected_cls",
    [
        ("invalid_client", InvalidClientError),
        ("invalid_grant", InvalidGrantError),
        ("unsupported_grant_type", UnsupportedGrantTypeError),
        ("authorization_pending", AuthorizationPendingError),
        ("slow_down", SlowDownError),
        ("access_denied", AccessDeniedError),
        ("expired_token", ExpiredTokenError),
    ],
)
def test_oauth_error_from_body_routes_code_to_subclass(
    rfc_code: str, expected_cls: type
) -> None:
    exc = oauth_error_from_body(
        status_code=400,
        body={"error": rfc_code, "error_description": "details"},
    )
    assert isinstance(exc, expected_cls)
    assert exc.error == rfc_code
    assert exc.error_description == "details"


def test_oauth_error_unknown_code_falls_back_to_oauth_error() -> None:
    exc = oauth_error_from_body(
        status_code=400, body={"error": "weird_code"}
    )
    assert type(exc) is OAuthError


def test_oauth_error_minimal_body_no_description() -> None:
    exc = oauth_error_from_body(status_code=400, body={"error": "invalid_grant"})
    assert exc.error == "invalid_grant"
    assert exc.error_description is None


def test_oauth_error_default_code_when_missing() -> None:
    exc = oauth_error_from_body(status_code=400, body={"error_description": "no code"})
    assert exc.error == "invalid_request"


# ---------- APIError __repr__ for log surface --------------------------


def test_api_error_repr_includes_code_and_status() -> None:
    exc = api_error_from_detail(
        status_code=429,
        detail={"error": "RATE_LIMITED", "message": "throttled", "next_action": "retry_after"},
    )
    s = repr(exc)
    assert "RateLimitedError" in s
    assert "429" in s
    assert "RATE_LIMITED" in s
    assert "retry_after" in s
