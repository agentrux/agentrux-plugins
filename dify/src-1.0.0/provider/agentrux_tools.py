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

        client_id = credentials.get("client_id") or ""
        client_secret = credentials.get("client_secret") or ""
        if not client_id or not client_secret:
            raise ToolProviderCredentialValidationError(
                "client_id and client_secret are required (use OAuth flow or paste Script credential)"
            )
        if not client_id.startswith("script_"):
            raise ToolProviderCredentialValidationError(
                "client_id must start with 'script_' (paste a Script credential ID, not a raw UUID)"
            )

        # Probe token endpoint with grant_type=client_credentials.
        try:
            resp = httpx.post(
                f"{base_url}/oauth/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                timeout=10,
            )
        except httpx.HTTPError as e:
            raise ToolProviderCredentialValidationError(
                f"AgenTrux API unreachable: {e}"
            ) from e
        if resp.status_code != 200:
            raise ToolProviderCredentialValidationError(
                f"AgenTrux rejected client_credentials (HTTP {resp.status_code})"
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
        return f"{base_url}/oauth/authorize?{urlencode(params)}"

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

        resp = httpx.post(f"{base_url}/oauth/token", data=data, timeout=10)
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

        resp = httpx.post(f"{base_url}/oauth/token", data=data, timeout=10)
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
