"""Tests for auth_models.py dataclass parsers.

Each dataclass has a `from_response()` constructor that validates the
server response shape; tests cover normal / error / boundary / attack
axes per memory `feedback_tests_max_strictness`.
"""
from __future__ import annotations

import pytest

from agentrux.sdk.auth_models import (
    ActivationCodeRedemption,
    AuthorizationServerMetadata,
    DCRRegistration,
    DeviceAuthorization,
    OAuthTokenResponse,
    PayloadDownload,
    PayloadUploadTicket,
)


# ---------- OAuthTokenResponse -----------------------------------------


def test_oauth_token_normal_client_credentials() -> None:
    r = OAuthTokenResponse.from_response(
        {"access_token": "aat_x", "token_type": "Bearer", "expires_in": 3600}
    )
    assert r.access_token == "aat_x"
    assert r.token_type == "Bearer"
    assert r.expires_in == 3600
    assert r.refresh_token is None


def test_oauth_token_with_refresh_and_scope() -> None:
    r = OAuthTokenResponse.from_response(
        {
            "access_token": "aat_x",
            "token_type": "Bearer",
            "expires_in": 60,
            "refresh_token": "art_y",
            "scope": "topic:abc:read topic:def:write",
        }
    )
    assert r.refresh_token == "art_y"
    assert r.scope == "topic:abc:read topic:def:write"


def test_oauth_token_missing_access_token_raises() -> None:
    with pytest.raises(ValueError, match="access_token"):
        OAuthTokenResponse.from_response({"token_type": "Bearer", "expires_in": 60})


def test_oauth_token_non_int_expires_in_raises() -> None:
    with pytest.raises(ValueError, match="expires_in"):
        OAuthTokenResponse.from_response(
            {"access_token": "aat", "token_type": "Bearer", "expires_in": "soon"}
        )


def test_oauth_token_default_token_type_bearer() -> None:
    r = OAuthTokenResponse.from_response(
        {"access_token": "aat_x", "expires_in": 60}
    )
    assert r.token_type == "Bearer"


def test_oauth_token_expires_at_monotonic_property() -> None:
    r = OAuthTokenResponse.from_response(
        {"access_token": "aat_x", "token_type": "Bearer", "expires_in": 120}
    )
    # Should be > received_at (some monotonic value)
    assert r.expires_at_monotonic > r.received_at
    assert r.expires_at_monotonic - r.received_at == pytest.approx(120, abs=0.01)


def test_oauth_token_expires_at_unix_uses_real_time() -> None:
    """expires_at_unix(reference_time=now) must produce a unix epoch
    timestamp comparable to int(time.time()), not a monotonic value.
    This guards against the bug Codex flagged in impl review #1."""
    import time

    r = OAuthTokenResponse.from_response(
        {"access_token": "aat_x", "token_type": "Bearer", "expires_in": 3600}
    )
    now_unix = int(time.time())
    unix = r.expires_at_unix()
    # Should be ~1h in the future on the unix epoch clock.
    assert now_unix + 3590 <= unix <= now_unix + 3610
    # Explicitly NOT a monotonic value (would be much smaller).
    assert unix > 10**9  # unix epoch is > 1 billion, monotonic is small


def test_oauth_token_expires_at_unix_explicit_reference() -> None:
    r = OAuthTokenResponse.from_response(
        {"access_token": "aat_x", "token_type": "Bearer", "expires_in": 60}
    )
    assert r.expires_at_unix(reference_time=1000) == 1060


# ---------- DCRRegistration --------------------------------------------


def _dcr_body(**overrides):
    base = {
        "client_id": "dcr_00000000-0000-0000-0000-000000000001",
        "client_id_issued_at": 1779600000,
        "client_secret_expires_at": 0,
        "client_name": "test-plugin",
        "redirect_uris": [],
        "grant_types": ["device_code", "refresh_token"],
        "token_endpoint_auth_method": "none",
        "registration_access_token": "rat_abc",
        "registration_client_uri": "/oauth/register/dcr_x",
    }
    base.update(overrides)
    return base


def test_dcr_normal() -> None:
    r = DCRRegistration.from_response(_dcr_body())
    assert r.client_id.startswith("dcr_")
    assert r.token_endpoint_auth_method == "none"
    assert r.registration_access_token.startswith("rat_")


def test_dcr_attack_wrong_prefix() -> None:
    with pytest.raises(ValueError, match="dcr_"):
        DCRRegistration.from_response(_dcr_body(client_id="oauth-client_xxx"))


def test_dcr_attack_empty_client_id() -> None:
    with pytest.raises(ValueError, match="dcr_"):
        DCRRegistration.from_response(_dcr_body(client_id=""))


def test_dcr_missing_field_raises() -> None:
    body = _dcr_body()
    body.pop("registration_access_token")
    with pytest.raises(ValueError, match="missing required field"):
        DCRRegistration.from_response(body)


# ---------- DeviceAuthorization ----------------------------------------


def _device_body(**overrides):
    base = {
        "device_code": "device_abc",
        "user_code": "ABCD-1234",
        "verification_uri": "https://example/device",
        "verification_uri_complete": "https://example/device?code=ABCD-1234",
        "expires_in": 600,
        "interval": 5,
    }
    base.update(overrides)
    return base


def test_device_auth_normal() -> None:
    d = DeviceAuthorization.from_response(_device_body())
    assert d.device_code == "device_abc"
    assert d.user_code == "ABCD-1234"
    assert d.expires_in == 600
    assert d.interval == 5


def test_device_auth_default_interval_5() -> None:
    body = _device_body()
    body.pop("interval")
    d = DeviceAuthorization.from_response(body)
    assert d.interval == 5


def test_device_auth_missing_device_code() -> None:
    body = _device_body()
    body.pop("device_code")
    with pytest.raises(ValueError, match="missing required field"):
        DeviceAuthorization.from_response(body)


# ---------- ActivationCodeRedemption -----------------------------------


def _ac_body(**overrides):
    base = {
        "client_id": "crd_00000000-0000-0000-0000-000000000001",
        "client_secret": "aks_secret_value",
        "script_id": "scr_00000000-0000-0000-0000-000000000001",
        "issued_at": "2026-05-24T10:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_ac_redemption_normal() -> None:
    r = ActivationCodeRedemption.from_response(_ac_body())
    assert r.client_id.startswith("crd_")
    assert r.client_secret.startswith("aks_")
    assert r.script_id.startswith("scr_")


@pytest.mark.parametrize(
    "field,bad_value,err_pattern",
    [
        ("client_id", "script_x", "crd_"),
        ("client_secret", "secret_only", "aks_"),
        ("script_id", "scripts_y", "scr_"),
    ],
)
def test_ac_redemption_attack_wrong_prefix(field: str, bad_value: str, err_pattern: str) -> None:
    with pytest.raises(ValueError, match=err_pattern):
        ActivationCodeRedemption.from_response(_ac_body(**{field: bad_value}))


# ---------- AuthorizationServerMetadata --------------------------------


def test_metadata_normal() -> None:
    m = AuthorizationServerMetadata.from_response(
        {
            "issuer": "https://api.agentrux.test",
            "authorization_endpoint": "https://api.agentrux.test/oauth/authorize",
            "token_endpoint": "https://api.agentrux.test/oauth/token",
            "registration_endpoint": "https://api.agentrux.test/oauth/register",
            "scopes_supported": ["topic:abc:read"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
        }
    )
    assert m.issuer.endswith(".test")
    assert m.token_endpoint.endswith("/oauth/token")
    assert "topic:abc:read" in m.scopes_supported


def test_metadata_minimal_required_only() -> None:
    m = AuthorizationServerMetadata.from_response(
        {
            "issuer": "x",
            "authorization_endpoint": "x/a",
            "token_endpoint": "x/t",
        }
    )
    assert m.revocation_endpoint is None
    assert m.scopes_supported == []


def test_metadata_missing_token_endpoint() -> None:
    with pytest.raises(ValueError, match="missing required field"):
        AuthorizationServerMetadata.from_response(
            {"issuer": "x", "authorization_endpoint": "x/a"}
        )


# ---------- PayloadUploadTicket / PayloadDownload -----------------------


def test_payload_upload_ticket_normal() -> None:
    t = PayloadUploadTicket.from_response(
        {
            "payload_object_id": "pob_00000000-0000-0000-0000-000000000001",
            "upload_url": "https://s3/sig",
            "upload_expires_at": "2026-05-24T11:00:00+00:00",
            "required_headers": {"x-amz-checksum-sha256": "abc=="},
        }
    )
    assert t.payload_object_id.startswith("pob_")
    assert t.required_headers["x-amz-checksum-sha256"] == "abc=="


def test_payload_upload_ticket_attack_wrong_prefix() -> None:
    with pytest.raises(ValueError, match="pob_"):
        PayloadUploadTicket.from_response(
            {
                "payload_object_id": "payload_x",
                "upload_url": "x",
                "upload_expires_at": "x",
            }
        )


def test_payload_download_normal() -> None:
    d = PayloadDownload.from_response(
        {
            "payload_object_id": "pob_00000000-0000-0000-0000-000000000001",
            "download_url": "https://s3/get",
            "download_expires_at": "2026-05-24T11:00:00+00:00",
            "content_type": "application/octet-stream",
            "size_bytes": 4096,
            "checksum_sha256": "deadbeef",
        }
    )
    assert d.size_bytes == 4096
    assert d.checksum_sha256 == "deadbeef"


def test_payload_download_optional_fields_default_none() -> None:
    d = PayloadDownload.from_response(
        {
            "payload_object_id": "pob_x_uuid",
            "download_url": "u",
            "download_expires_at": "t",
        }
    )
    assert d.content_type is None
    assert d.size_bytes is None
