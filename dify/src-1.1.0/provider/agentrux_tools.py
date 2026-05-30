"""AgenTrux Tool Provider — OAuth 2.1 + client_credentials fallback.

Dual-mode auth:
  - OAuth Authorization Code + PKCE (Dify 1.10+ oauth_schema)
      Dify drives the consent flow; tokens are stored by Dify and refreshed
      automatically via _oauth_refresh_credentials.
  - client_credentials (credentials_for_provider fallback)
      User pastes script_<uuid> + client_secret directly. Plugin runtime
      exchanges them for a JWT on each tool call (cached in-memory).
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
from dify_plugin import ToolProvider
from dify_plugin.entities.oauth import ToolOAuthCredentials
from dify_plugin.errors.tool import (
    ToolProviderCredentialValidationError,
    ToolProviderOAuthError,
)
from werkzeug import Request


# ---------------------------------------------------------------------------
# URL validation (HTTPS or loopback only — never plaintext public URLs)
# ---------------------------------------------------------------------------

def _is_url_allowed(base_url: str) -> bool:
    if base_url.startswith("https://"):
        return True
    if base_url.startswith("http://localhost") or base_url.startswith(
        "http://127.0.0.1"
    ):
        return True
    return False


# ---------------------------------------------------------------------------
# PKCE helpers (S256)
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge_S256)."""
    import base64
    import hashlib

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# RFC 8414 metadata discovery
# ---------------------------------------------------------------------------
#
# Plugins used to hardcode `{base_url}/oauth/authorize` and
# `{base_url}/oauth/token`. Switching to RFC 8414
# `/.well-known/oauth-authorization-server` discovery means the plugin
# follows whatever URLs the AgenTrux backend advertises today —
# including future moves like splitting the consent UI to a separate
# host. The endpoints are cached per base_url for the process lifetime
# (TTL implicit: discovery rarely changes; a Dify worker restart
# refetches anyway).

_metadata_cache: dict[str, dict[str, str]] = {}


def _discover_metadata(base_url: str) -> dict[str, str]:
    cached = _metadata_cache.get(base_url)
    if cached is not None:
        return cached
    resp = httpx.get(
        f"{base_url}/.well-known/oauth-authorization-server",
        timeout=10,
    )
    if resp.status_code != 200:
        # Fall back to the legacy hardcoded layout so an outage of the
        # well-known endpoint doesn't take down auth flows that already
        # know where the endpoints live.
        meta = {
            "authorization_endpoint": f"{base_url}/oauth/authorize",
            "token_endpoint": f"{base_url}/oauth/token",
        }
    else:
        body = resp.json()
        meta = {
            "authorization_endpoint": body.get(
                "authorization_endpoint", f"{base_url}/oauth/authorize",
            ),
            "token_endpoint": body.get(
                "token_endpoint", f"{base_url}/oauth/token",
            ),
        }
    _metadata_cache[base_url] = meta
    return meta


# In-process map: state -> code_verifier. Sufficient because Dify keeps the
# user on the same plugin process between authorize and callback within a
# single OAuth round-trip.
_pkce_state: dict[str, str] = {}


class AgentruxToolsProvider(ToolProvider):
    # -----------------------------------------------------------------
    # client_credentials path (credentials_for_provider)
    # -----------------------------------------------------------------
    def _validate_credentials(self, credentials: dict) -> None:
        # If access_token is present (OAuth path completed), nothing to do —
        # Dify already validated through _oauth_get_credentials.
        if credentials.get("access_token"):
            return

        base_url = credentials.get("base_url") or ""
        if not base_url:
            raise ToolProviderCredentialValidationError("base_url is required")
        if not _is_url_allowed(base_url):
            raise ToolProviderCredentialValidationError(
                "base_url must use HTTPS (or http://localhost for development)"
            )

        from .agentrux_api import _client_credentials_token, validate_activation

        # Activation Code path: redeem act_ -> Script credential (crd_/aks_),
        # idempotent via the activation-code fingerprint cache, then probe
        # client_credentials.
        activation_code = credentials.get("activation_code") or ""
        if activation_code:
            try:
                client_id, client_secret = validate_activation(base_url, activation_code)
                _client_credentials_token(base_url, client_id, client_secret)
            except httpx.HTTPStatusError as e:
                raise ToolProviderCredentialValidationError(
                    f"Activation failed (HTTP {e.response.status_code}): the code may be "
                    "expired, already consumed, or the Script suspended. Issue a fresh "
                    "Activation Code in the AgenTrux Console."
                ) from e
            except httpx.HTTPError as e:
                raise ToolProviderCredentialValidationError(
                    f"AgenTrux API unreachable: {e}"
                ) from e
            return

        # Back-compat: an explicit Script credential (crd_/aks_) supplied
        # directly in this credential set. (No base_url-only cache fallback —
        # that could validate against another Script's credential on a shared
        # API host; Codex impl review Q3.)
        client_id = credentials.get("client_id") or ""
        client_secret = credentials.get("client_secret") or ""
        if client_id and client_secret:
            try:
                _client_credentials_token(base_url, client_id, client_secret)
            except httpx.HTTPError as e:
                raise ToolProviderCredentialValidationError(
                    f"Script credential rejected: {e}"
                ) from e
            return

        raise ToolProviderCredentialValidationError(
            "Provide an Activation Code (act_...) from the AgenTrux Console, "
            "or connect via the OAuth flow."
        )

    # -----------------------------------------------------------------
    # OAuth Authorization Code + PKCE path (oauth_schema)
    # -----------------------------------------------------------------
    def _oauth_get_authorization_url(
        self, redirect_uri: str, system_credentials: Mapping[str, Any]
    ) -> str:
        base_url = system_credentials.get("base_url") or "https://api.agentrux.com"
        client_id = system_credentials.get("client_id") or ""
        if not _is_url_allowed(base_url):
            raise ToolProviderOAuthError(
                f"base_url must use HTTPS (got {base_url!r})"
            )
        if not client_id:
            raise ToolProviderOAuthError("OAuth client_id is required")

        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        _pkce_state[state] = verifier

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": "topic.read topic.write",
        }
        meta = _discover_metadata(base_url)
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    def _oauth_get_credentials(
        self,
        redirect_uri: str,
        system_credentials: Mapping[str, Any],
        request: Request,
    ) -> ToolOAuthCredentials:
        base_url = system_credentials.get("base_url") or "https://api.agentrux.com"
        client_id = system_credentials.get("client_id") or ""
        client_secret = system_credentials.get("client_secret") or ""

        code = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")
        if error:
            raise ToolProviderOAuthError(
                f"AgenTrux authorization denied: {error} ({request.args.get('error_description', '')})"
            )
        if not code or not state:
            raise ToolProviderOAuthError(
                "OAuth callback missing 'code' or 'state' parameter"
            )
        verifier = _pkce_state.pop(state, None)
        if not verifier:
            raise ToolProviderOAuthError("OAuth state mismatch (PKCE verifier missing)")

        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
        if client_secret:
            data["client_secret"] = client_secret

        meta = _discover_metadata(base_url)
        resp = httpx.post(meta["token_endpoint"], data=data, timeout=10)
        if resp.status_code != 200:
            raise ToolProviderOAuthError(
                f"AgenTrux token exchange failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        return _to_oauth_credentials(base_url, body)

    def _oauth_refresh_credentials(
        self,
        redirect_uri: str,
        system_credentials: Mapping[str, Any],
        credentials: Mapping[str, Any],
    ) -> ToolOAuthCredentials:
        base_url = (
            credentials.get("base_url")
            or system_credentials.get("base_url")
            or "https://api.agentrux.com"
        )
        client_id = system_credentials.get("client_id") or ""
        client_secret = system_credentials.get("client_secret") or ""
        refresh_token = credentials.get("refresh_token") or ""
        if not refresh_token:
            raise ToolProviderOAuthError("No refresh_token to refresh with")

        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret

        meta = _discover_metadata(base_url)
        resp = httpx.post(meta["token_endpoint"], data=data, timeout=10)
        if resp.status_code != 200:
            raise ToolProviderOAuthError(
                f"AgenTrux refresh failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        return _to_oauth_credentials(base_url, body, fallback_refresh=refresh_token)


def _to_oauth_credentials(
    base_url: str, body: dict, fallback_refresh: str = ""
) -> ToolOAuthCredentials:
    expires_in = int(body.get("expires_in", 3600))
    expires_at = int(time.time()) + expires_in
    return ToolOAuthCredentials(
        credentials={
            "base_url": base_url,
            "access_token": body.get("access_token", ""),
            "refresh_token": body.get("refresh_token") or fallback_refresh,
            "expires_at": str(expires_at),
            "scope": body.get("scope", ""),
        },
        expires_at=expires_at,
    )
