"""HTTPClient — httpx wrapper with retry / backoff + auth-aware request_with_auth.

SSOT: docs/04_design/sdk/sdk_design.md §3-3 / §8
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

import httpx

from agentrux_sdk.config import SDKConfig
from agentrux_sdk.errors import (
    CredentialRotatedError,
    RateLimitError,
    ServerError,
    TemporaryError,
)

if TYPE_CHECKING:
    from agentrux_sdk.auth import Authenticator


class HTTPClient:
    """async httpx.AsyncClient の薄い wrapper.

    責務:
      - timeout 一元化 (config から)
      - User-Agent / 共通 header 付与
      - retry: TemporaryError / RateLimitError / httpx 一過性エラーで exponential backoff
      - auth: request_with_auth() で 401 invalid_token 再 issue + retry 1 回

    SSOT: docs/04_design/sdk/sdk_design.md §3-3 (auth fallback) / §8 (retry policy)
    """

    def __init__(self, config: SDKConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(
                connect=config.connect_timeout, read=config.read_timeout, write=10.0, pool=5.0
            ),
            headers={"User-Agent": config.user_agent},
        )

    # ------------------------------------------------------------------
    # raw / authless
    # ------------------------------------------------------------------
    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self._client.request(method, path, **kwargs)

    # ------------------------------------------------------------------
    # auth-aware (401 invalid_token → force_refresh → 1 回限り retry)
    # ------------------------------------------------------------------
    async def request_with_auth(
        self,
        method: str,
        path: str,
        *,
        auth: Authenticator,
        **kwargs: Any,
    ) -> httpx.Response:
        aat = await auth.get_access_token()
        headers = kwargs.pop("headers", None) or {}
        headers["Authorization"] = f"Bearer {aat}"
        r = await self.request(method, path, headers=headers, **kwargs)
        if r.status_code != 401:
            return r

        # 401: client rotated? → 即 raise (CredentialRotatedError は force_refresh で投げる)
        try:
            err_code = r.json().get("error", "")
        except Exception:
            err_code = ""
        if err_code == "invalid_client":
            raise CredentialRotatedError(
                "client_secret was rotated or revoked; re-onboard via activation_code"
            )
        # invalid_token / 他: 1 回だけ強制再 issue → retry
        aat2 = await auth.force_refresh()
        headers["Authorization"] = f"Bearer {aat2}"
        return await self.request(method, path, headers=headers, **kwargs)

    # ------------------------------------------------------------------
    # retry wrapper
    # ------------------------------------------------------------------
    async def request_with_retry(
        self,
        method: str,
        path: str,
        *,
        auth: Authenticator | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """retry on TemporaryError / RateLimitError / network error.

        - auth が渡されたら request_with_auth 経由、 そうでなければ raw request
        - max_retries: config.max_retries (default 3)
        - backoff: exponential (base 0.5、 ×2、 jitter ±25%)
        - 429 は Retry-After header 優先
        """
        attempt = 0
        last_exc: Exception | None = None
        while attempt <= self._config.max_retries:
            try:
                if auth is not None:
                    r = await self.request_with_auth(method, path, auth=auth, **kwargs)
                else:
                    r = await self.request(method, path, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
                last_exc = exc
                attempt += 1
                if attempt > self._config.max_retries:
                    raise TemporaryError(f"network error after {attempt - 1} retries: {exc}") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            if r.status_code == 429:
                retry_after = self._parse_retry_after(r)
                attempt += 1
                if attempt > self._config.max_retries:
                    raise RateLimitError("rate limited", retry_after=retry_after)
                await asyncio.sleep(retry_after if retry_after is not None else self._backoff(attempt))
                continue

            if r.status_code in (502, 503, 504):
                attempt += 1
                if attempt > self._config.max_retries:
                    raise ServerError(f"upstream {r.status_code} after {attempt - 1} retries")
                await asyncio.sleep(self._backoff(attempt))
                continue

            return r

        # unreachable: loop は必ず raise or return
        raise TemporaryError(f"max retries exceeded ({self._config.max_retries})") from last_exc

    def _backoff(self, attempt: int) -> float:
        """exponential with ±25% jitter."""
        base = self._config.retry_base_seconds * (2 ** (attempt - 1))
        jitter = base * 0.25 * (random.random() * 2 - 1)
        return max(0.01, base + jitter)

    @staticmethod
    def _parse_retry_after(r: httpx.Response) -> float | None:
        value = r.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HTTPClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
