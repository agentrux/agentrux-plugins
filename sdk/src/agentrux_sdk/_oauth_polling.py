"""RFC 8628 §3.4 device flow polling primitive (shared).

SSOT: docs/04_design/auth/device_code_setup_v1.md §5-1 (Step 1a 抽出のみ、 挙動不変)

`topology_install.py` の `_poll_token()` から **polling loop 部分のみ** を抽出。
返り値は raw 200 OK body (dict)、 caller が flow 別に parse する設計:
- Topology Flow v1: `topology_install._parse_token_response()` で `InstallResult` に変換
- Plain device code: `device_code_setup._parse_token_response()` で `DeviceCodeSetupResult` に変換

Step 1b (別 PR) で jitter / 429 Retry-After / connect retry を追加する余地を残しつつ、
v1 は **既存 28 件 test が変更なしで pass する pure-move** とする。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from agentrux_sdk._oauth_errors import (
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallTimeoutError,
    parse_oauth_error,
)
from agentrux_sdk.errors import AgenTruxError

# Polling constants (RFC 8628 §3.5 + AgenTrux server 整合)
MIN_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 60.0
SLOW_DOWN_INCREMENT = 5

# Endpoint constants (公開しない、 sdk 内 helper 用)
_TOKEN_PATH = "/oauth/token"  # noqa: S105 (RFC 6749 endpoint name, not a password)
_GRANT_TYPE_DEVICE_CODE = "device_code"  # AgenTrux dispatcher 仕様 (literal、 URN ではない)


async def poll_device_token(
    *,
    http: httpx.AsyncClient,
    client_id: str,
    device_code: str,
    user_code: str,
    timeout_s: int,
    initial_interval: float,
) -> dict[str, Any]:
    """RFC 8628 §3.4 polling loop。 200 成功時に raw body (dict) を返す.

    Args:
        http: 共有 httpx.AsyncClient (caller 管理、 base_url 設定済前提)
        client_id: OAuth public client UUID
        device_code: `/oauth/device/authorize` or `/oauth/topology-request` で取得
        user_code: error message 内表示用 (raw bearer の device_code を漏らさない)
        timeout_s: 全体 deadline 秒 (caller が device_code TTL ≤600 を保証)
        initial_interval: server `interval` 値 (≧ MIN_POLL_INTERVAL に丸める)

    Returns:
        200 OK body dict (caller が flow 別 parse する)

    Raises:
        InstallTimeoutError: timeout 超過 or RFC 8628 `expired_token`
        InstallDeniedError: RFC 8628 `access_denied`
        InstallAuthError: RFC 8628 `invalid_client`
        InstallError: `invalid_grant` 系 / unexpected error code
        AgenTruxError: network error / unexpected status
    """
    deadline = time.monotonic() + timeout_s
    interval = max(MIN_POLL_INTERVAL, float(initial_interval))
    while True:
        if time.monotonic() >= deadline:
            raise InstallTimeoutError(
                f"approval not completed within {timeout_s}s (user_code={user_code})"
            )
        await asyncio.sleep(interval)

        data = {
            "grant_type": _GRANT_TYPE_DEVICE_CODE,
            "device_code": device_code,
            "client_id": client_id,
        }
        try:
            r = await http.post(
                _TOKEN_PATH,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise AgenTruxError(f"network error during token poll: {exc}") from exc

        if r.status_code == 200:
            return r.json()

        code, desc = parse_oauth_error(r)
        # RFC 8628 §3.5 standard errors
        if code == "authorization_pending":
            continue
        if code == "slow_down":
            interval = min(MAX_POLL_INTERVAL, interval + SLOW_DOWN_INCREMENT)
            continue
        if code == "access_denied":
            raise InstallDeniedError(
                f"user denied the request (user_code={user_code})"
            )
        if code == "expired_token":
            raise InstallTimeoutError(
                f"device_code expired (user_code={user_code})"
            )
        if code == "invalid_grant":
            raise InstallError(f"invalid_grant: {desc}")
        if code == "invalid_client":
            raise InstallAuthError(f"invalid_client: {desc}")
        raise AgenTruxError(
            f"unexpected token response status={r.status_code} "
            f"error={code!r} description={desc!r}"
        )


__all__ = [
    "MAX_POLL_INTERVAL",
    "MIN_POLL_INTERVAL",
    "SLOW_DOWN_INCREMENT",
    "poll_device_token",
]
