"""Authenticator — 経路 B (client_credentials) 専用 access_token cache + refresh.

SSOT: docs/04_design/sdk/sdk_design.md §3-2 / §3-3
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from agentrux_sdk.config import SDKConfig
from agentrux_sdk.errors import (
    AgenTruxError,
    AuthenticationError,
    ConfigError,
    CredentialRotatedError,
)
from agentrux_sdk.http_client import HTTPClient


class Authenticator:
    """access_token を 1 つだけ in-memory に保持し、 必要に応じて再 issue.

    invariant (sdk_design.md §3-2):
      - access_token は in-memory のみ
      - client_secret は constructor 入力で in-memory に保持
      - 401 invalid_client は CredentialRotatedError、 401 invalid_token は 1 回限り再 issue
    """

    _TOKEN_PATH = "/oauth/token"

    def __init__(self, config: SDKConfig, http: HTTPClient) -> None:
        self._config = config
        self._http = http
        self._access_token: str | None = None
        self._expires_at: datetime | None = None  # tz-aware UTC
        self._lock = asyncio.Lock()  # 並列 publish/read が同時に re-issue するのを防ぐ

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    async def get_access_token(self) -> str:
        """有効な aat_ を返す. expires_at の lead 内なら先行再 issue."""
        async with self._lock:
            if self._is_valid_with_lead():
                assert self._access_token is not None
                return self._access_token
            return await self._issue_locked()

    async def force_refresh(self) -> str:
        """401 fallback / 強制 re-issue."""
        async with self._lock:
            return await self._issue_locked()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _is_valid_with_lead(self) -> bool:
        if self._access_token is None or self._expires_at is None:
            return False
        lead = timedelta(seconds=self._config.refresh_lead_seconds)
        return datetime.now(UTC) + lead < self._expires_at

    async def _issue_locked(self) -> str:
        """POST /oauth/token grant_type=client_credentials.

        Caller must hold self._lock.
        """
        data = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        try:
            r = await self._http.request(
                "POST",
                self._TOKEN_PATH,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise AgenTruxError(f"network error during token issue: {exc}") from exc

        if r.status_code == 200:
            body = r.json()
            self._access_token = body["access_token"]
            self._expires_at = datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))
            return self._access_token

        # error mapping
        try:
            err = r.json()
            error_code = err.get("error", "")
        except Exception:
            error_code = ""

        if r.status_code == 401 and error_code == "invalid_client":
            raise CredentialRotatedError(
                "client_secret was rotated or revoked; re-onboard via activation_code"
            )
        if r.status_code == 401:
            raise AuthenticationError(f"invalid client_credentials: {error_code or r.text}")
        if r.status_code == 400 and error_code == "invalid_request":
            raise ConfigError(f"invalid token request: {err.get('error_description', '')}")
        raise AgenTruxError(f"token endpoint returned {r.status_code}: {r.text}")
