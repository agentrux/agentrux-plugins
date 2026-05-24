"""OAuth + payload-related dataclasses for AgenTrux SDK.

Mirrors AgenTrux server's auth flows (OAuth 2.1 + RFC 8628 device flow +
RFC 7591 DCR + RFC 8414 metadata discovery) and the Topic payload presigned
URL flow.

Server SSOT:
  AgenTrux/src/agentrux/api/routers/oauth_router.py
  AgenTrux/src/agentrux/api/routers/device_flow_router.py
  AgenTrux/src/agentrux/api/routers/activation_code_router.py
  AgenTrux/src/agentrux/api/routers/well_known_router.py
  AgenTrux/src/agentrux/api/routers/pipe_router.py:890 (POST /payloads)

Prefix conventions:
  dcr_<uuid>      DCR-registered public OAuth client (device_flow_router.py:441)
  rat_<base64>    registration_access_token (device_flow_router.py:442)
  crd_<uuid>      confidential script credential client_id (Phase 1.9b)
  aks_<base64>    confidential script credential client_secret (一度限り発行)
  act_<base64>    activation code (one-shot consumption)
  aat_<JWT>       access token
  art_<JWT>       refresh token
  pob_<uuid>      payload object id
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


def _coerce_int(value: Any, field_name: str) -> int:
    """Best-effort int coerce with informative error."""
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be int-convertible, got {value!r}") from exc


# ---------- OAuth token response (POST /oauth/token) ----------


@dataclass(frozen=True)
class OAuthTokenResponse:
    """Common shape of POST /oauth/token success response.

    Server emits this for all three grant_types:
      - client_credentials (crd_/aks_  → aat_, no refresh_token)
      - authorization_code (PKCE/device flow → aat_ + art_)
      - refresh_token      (art_ rotation → new aat_ + new art_)
    """

    access_token: str                 # "aat_<JWT>"
    token_type: str                   # "Bearer"
    expires_in: int                   # seconds
    refresh_token: str | None = None  # "art_<JWT>" (when issued)
    scope: str | None = None          # space-separated scope string
    id_token: str | None = None       # OIDC id_token (openid scope 時)
    # SDK-local: arrival timestamp, used by JWT auto-refresh planning.
    received_at: float = field(default_factory=time.monotonic)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> OAuthTokenResponse:
        try:
            access_token = str(data["access_token"])
            token_type = str(data.get("token_type", "Bearer"))
            expires_in = _coerce_int(data["expires_in"], "expires_in")
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc
        return cls(
            access_token=access_token,
            token_type=token_type,
            expires_in=expires_in,
            refresh_token=data.get("refresh_token"),
            scope=data.get("scope"),
            id_token=data.get("id_token"),
        )

    @property
    def expires_at_monotonic(self) -> float:
        """When this token expires, on monotonic clock (drift-resistant).

        DO NOT pass this to TokenManager.explicit_expires_at_unix — use
        `expires_at_unix(reference_time=int(time.time()))` for that.
        Monotonic and unix epoch are two different clocks; mixing them
        looks superficially fine but produces years-off comparisons.
        """
        return self.received_at + self.expires_in

    def expires_at_unix(self, *, reference_time: int | None = None) -> int:
        """When this token expires, in unix epoch seconds.

        `reference_time` defaults to `int(time.time())` at the moment of
        this call — pass an earlier captured value if you want to be
        strict about clock skew (subtract the response RTT).
        """
        import time as _time
        ref = reference_time if reference_time is not None else int(_time.time())
        return ref + self.expires_in


# ---------- DCR (POST /oauth/register, RFC 7591) ----------


@dataclass(frozen=True)
class DCRRegistration:
    """Response of POST /oauth/register (device_flow_router.py:430).

    The server only supports `token_endpoint_auth_method=none` (public
    clients for device flow / MCP), so client_secret is never returned.
    """

    client_id: str                              # "dcr_<uuid>"
    client_id_issued_at: int                    # unix epoch seconds
    client_secret_expires_at: int               # 0 = never (public client)
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    token_endpoint_auth_method: str             # always "none"
    registration_access_token: str              # "rat_<base64>"
    registration_client_uri: str                # "/oauth/register/<client_id>"

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> DCRRegistration:
        try:
            client_id = str(data["client_id"])
            if not client_id.startswith("dcr_"):
                raise ValueError(
                    f"client_id must start with 'dcr_', got {client_id!r}"
                )
            return cls(
                client_id=client_id,
                client_id_issued_at=_coerce_int(
                    data["client_id_issued_at"], "client_id_issued_at"
                ),
                client_secret_expires_at=_coerce_int(
                    data.get("client_secret_expires_at", 0),
                    "client_secret_expires_at",
                ),
                client_name=str(data["client_name"]),
                redirect_uris=list(data.get("redirect_uris", [])),
                grant_types=list(data.get("grant_types", [])),
                token_endpoint_auth_method=str(
                    data.get("token_endpoint_auth_method", "none")
                ),
                registration_access_token=str(data["registration_access_token"]),
                registration_client_uri=str(data["registration_client_uri"]),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc


# ---------- Device flow (POST /oauth/device_authorization, RFC 8628) ----------


@dataclass(frozen=True)
class DeviceAuthorization:
    """Response of POST /oauth/device_authorization.

    User visits `verification_uri_complete` (or `verification_uri` + types
    in `user_code`), and the SDK polls POST /oauth/token grant_type=
    urn:ietf:params:oauth:grant-type:device_code until completion.
    """

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int                   # device_code TTL, seconds
    interval: int                     # min polling interval, seconds

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> DeviceAuthorization:
        try:
            return cls(
                device_code=str(data["device_code"]),
                user_code=str(data["user_code"]),
                verification_uri=str(data["verification_uri"]),
                verification_uri_complete=data.get("verification_uri_complete"),
                expires_in=_coerce_int(data["expires_in"], "expires_in"),
                interval=_coerce_int(data.get("interval", 5), "interval"),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc


# ---------- Activation Code (POST /auth/redeem-activation-code, Phase 1.9) ----------


@dataclass(frozen=True)
class ActivationCodeRedemption:
    """Response of POST /auth/redeem-activation-code.

    Returns a confidential client_id + client_secret pair; the secret is
    one-shot and is the only time it is ever returned in plaintext. SDK
    callers MUST persist it immediately to a 0600 file.
    """

    client_id: str                    # "crd_<uuid>"
    client_secret: str                # "aks_<base64>" (1 度限り)
    script_id: str                    # "scr_<uuid>"
    issued_at: str                    # ISO datetime

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> ActivationCodeRedemption:
        try:
            client_id = str(data["client_id"])
            client_secret = str(data["client_secret"])
            script_id = str(data["script_id"])
            if not client_id.startswith("crd_"):
                raise ValueError(
                    f"client_id must start with 'crd_', got {client_id!r}"
                )
            if not client_secret.startswith("aks_"):
                raise ValueError(
                    f"client_secret must start with 'aks_'"
                )
            if not script_id.startswith("scr_"):
                raise ValueError(
                    f"script_id must start with 'scr_', got {script_id!r}"
                )
            return cls(
                client_id=client_id,
                client_secret=client_secret,
                script_id=script_id,
                issued_at=str(data["issued_at"]),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc


# ---------- RFC 8414 Authorization Server Metadata (GET /.well-known/oauth-authorization-server) ----------


@dataclass(frozen=True)
class AuthorizationServerMetadata:
    """Subset of RFC 8414 fields the SDK actually uses for endpoint discovery."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str | None = None
    introspection_endpoint: str | None = None
    registration_endpoint: str | None = None  # DCR
    device_authorization_endpoint: str | None = None
    jwks_uri: str | None = None
    scopes_supported: list[str] = field(default_factory=list)
    grant_types_supported: list[str] = field(default_factory=list)

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> AuthorizationServerMetadata:
        try:
            return cls(
                issuer=str(data["issuer"]),
                authorization_endpoint=str(data["authorization_endpoint"]),
                token_endpoint=str(data["token_endpoint"]),
                revocation_endpoint=data.get("revocation_endpoint"),
                introspection_endpoint=data.get("introspection_endpoint"),
                registration_endpoint=data.get("registration_endpoint"),
                device_authorization_endpoint=data.get(
                    "device_authorization_endpoint"
                ),
                jwks_uri=data.get("jwks_uri"),
                scopes_supported=list(data.get("scopes_supported", [])),
                grant_types_supported=list(data.get("grant_types_supported", [])),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc


# ---------- Payload upload / download (POST/GET /topics/{id}/payloads) ----------


@dataclass(frozen=True)
class PayloadUploadTicket:
    """Response of POST /topics/{id}/payloads (pipe_router.py:890).

    The SDK then performs a PUT to `upload_url` (S3/MinIO presigned URL)
    with the actual bytes, and afterwards calls publish_event with
    payload_object_id to attach the uploaded object to a Topic event.
    """

    payload_object_id: str            # "pob_<uuid>"
    upload_url: str                   # presigned PUT URL (signed by S3/MinIO)
    upload_expires_at: str            # ISO datetime
    required_headers: dict[str, str]  # e.g. {"x-amz-checksum-sha256": "<base64>"}

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> PayloadUploadTicket:
        try:
            pob = str(data["payload_object_id"])
            if not pob.startswith("pob_"):
                raise ValueError(f"payload_object_id must start with 'pob_'")
            return cls(
                payload_object_id=pob,
                upload_url=str(data["upload_url"]),
                upload_expires_at=str(data["upload_expires_at"]),
                required_headers=dict(data.get("required_headers", {})),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc


@dataclass(frozen=True)
class PayloadDownload:
    """Response of GET /topics/{id}/payloads/{pob_id} (pipe_router.py:984).

    Returns a presigned GET URL for the SDK / app to fetch the object
    directly from S3/MinIO.
    """

    payload_object_id: str
    download_url: str
    download_expires_at: str
    content_type: str | None = None
    size_bytes: int | None = None
    checksum_sha256: str | None = None  # hex

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> PayloadDownload:
        try:
            pob = str(data["payload_object_id"])
            if not pob.startswith("pob_"):
                raise ValueError(f"payload_object_id must start with 'pob_'")
            return cls(
                payload_object_id=pob,
                download_url=str(data["download_url"]),
                download_expires_at=str(data["download_expires_at"]),
                content_type=data.get("content_type"),
                size_bytes=(
                    int(data["size_bytes"]) if data.get("size_bytes") is not None else None
                ),
                checksum_sha256=data.get("checksum_sha256"),
            )
        except KeyError as exc:
            raise ValueError(f"missing required field: {exc.args[0]!r}") from exc
