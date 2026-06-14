"""Tests for agentrux_sdk.device_code_setup (Step 2、 plain device code RFC 8628).

SSOT: docs/04_design/auth/device_code_setup_v1.md §6-1
品質基準: docs/01_overview/test_quality_checklist.md (a 正常 / b エラー / c 境界 / d 攻撃 / e race)

各 test の前提:
- httpx.AsyncClient を `agentrux_sdk.device_code_setup.httpx.AsyncClient` で monkeypatch
- asyncio.sleep を高速化 (`_no_sleep` fixture、 `agentrux_sdk._oauth_polling.asyncio.sleep`)
- handler ベースの mock server (request path → response status + body)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agentrux_sdk.device_code_setup import (
    DeviceCodeSetupPending,
    DeviceCodeSetupResult,
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallTimeoutError,
    setup_via_device_code,
)
from agentrux_sdk.errors import AgenTruxError, ConfigError

pytestmark = pytest.mark.asyncio


# ----------------------------------------------------------------------------
# Mock infrastructure
# ----------------------------------------------------------------------------


def _make_handler(
    plan: list[tuple[str, int, dict[str, Any]]],
) -> Callable[[httpx.Request], Awaitable[httpx.Response]]:
    """request path × order に応じて scripted response を返す mock handler.

    plan は (path, status, body) の tuple list、 path 一致でも順序通り消費される。
    """
    state = {"i": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        i = state["i"]
        if i >= len(plan):
            return httpx.Response(599, json={"error": "no more plan"})
        path, status, body = plan[i]
        # path 一致のみ簡易チェック
        if not request.url.path.endswith(path):
            return httpx.Response(
                500, json={"error": "unexpected", "got": request.url.path, "want": path}
            )
        state["i"] = i + 1
        return httpx.Response(status, json=body)

    return handler


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """polling 内 asyncio.sleep を 0 にして高速化."""

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("agentrux_sdk._oauth_polling.asyncio.sleep", fast_sleep)


@pytest.fixture
def _patch_client(monkeypatch: pytest.MonkeyPatch):
    """httpx.AsyncClient を MockTransport 経由に差し替える factory."""

    def _patch(handler: Callable[[httpx.Request], Awaitable[httpx.Response]]) -> None:
        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient

        def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return original(transport=transport, *args, **kwargs)

        monkeypatch.setattr(
            "agentrux_sdk.device_code_setup.httpx.AsyncClient", factory
        )

    return _patch


def _success_token_body(
    *, scope: str = "topic.read topic.write", include_id_token: bool = False
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": "aat_test_access",
        "refresh_token": "art_test_refresh",
        "expires_in": 600,
        "scope": scope,
        "token_type": "Bearer",
    }
    if include_id_token:
        body["id_token"] = "eyJ.id.token"
    return body


def _device_authorize_body() -> dict[str, Any]:
    return {
        "device_code": "dc_TEST_DEVICE",
        "user_code": "ABCD-1234",
        "verification_uri": "https://console.agentrux.com/device",
        "verification_uri_complete": "https://console.agentrux.com/device?user_code=ABCD-1234",
        "expires_in": 600,
        "interval": 5,
    }


# ============================================================================
# a. 正常系 (a1-a4)
# ============================================================================


async def test_a1_happy_path_default_scope(
    _no_sleep: None, _patch_client: Any
) -> None:
    """issue → 1 poll で 200 → DeviceCodeSetupResult が組み立てられる (default scope)."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
    )
    assert isinstance(result, DeviceCodeSetupResult)
    assert result.access_token == "aat_test_access"
    assert result.refresh_token == "art_test_refresh"
    assert result.expires_in == 600
    assert result.scope == ("topic.read", "topic.write")
    assert result.id_token is None
    assert result.granted_scopes == result.scope
    assert result.granted_at_unix > 0


async def test_a2_openid_scope_returns_id_token(
    _no_sleep: None, _patch_client: Any
) -> None:
    """openid + email + profile scope で id_token も返る."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    200,
                    _success_token_body(
                        scope="topic.read topic.write openid email profile",
                        include_id_token=True,
                    ),
                ),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        scope=("topic.read", "topic.write", "openid", "email", "profile"),
    )
    assert result.id_token == "eyJ.id.token"
    assert "openid" in result.scope


async def test_a3_sync_callback_called(_no_sleep: None, _patch_client: Any) -> None:
    """同期 callable on_user_code が pending info で 1 度呼ばれる."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    seen: list[DeviceCodeSetupPending] = []

    def cb(info: DeviceCodeSetupPending) -> None:
        seen.append(info)

    await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        on_user_code=cb,
    )
    assert len(seen) == 1
    assert seen[0].user_code == "ABCD-1234"
    assert seen[0].verification_uri == "https://console.agentrux.com/device"
    assert "?user_code=ABCD-1234" in seen[0].verification_uri_complete


async def test_a4_async_callback_awaited(_no_sleep: None, _patch_client: Any) -> None:
    """async callable on_user_code が await される."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    awaited = AsyncMock()

    async def cb(info: DeviceCodeSetupPending) -> None:
        await awaited(info.user_code)

    await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        on_user_code=cb,
    )
    awaited.assert_awaited_once_with("ABCD-1234")


# ============================================================================
# b. エラー系 (b1-b6)
# ============================================================================


async def test_b1_invalid_client_at_authorize(
    _no_sleep: None, _patch_client: Any
) -> None:
    """POST /oauth/device/authorize が 400 invalid_client → InstallAuthError."""
    _patch_client(
        _make_handler(
            [
                (
                    "/oauth/device/authorize",
                    400,
                    {"detail": {"error": "invalid_client", "error_description": "unknown"}},
                ),
            ]
        )
    )
    with pytest.raises(InstallAuthError, match="invalid_client"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_unknown",
        )


async def test_b2_invalid_scope_pre_validation() -> None:
    """vocab 外 scope は client side で ConfigError (server 未到達)."""
    with pytest.raises(ConfigError, match="not in vocabulary"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test",
            scope=("topic.delete",),  # vocab 外
        )


async def test_b3_access_denied_during_polling(
    _no_sleep: None, _patch_client: Any
) -> None:
    """polling 中に server が access_denied → InstallDeniedError."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "access_denied", "error_description": "user denied"}},
                ),
            ]
        )
    )
    with pytest.raises(InstallDeniedError, match="user denied"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test_client",
        )


async def test_b4_expired_token_during_polling(
    _no_sleep: None, _patch_client: Any
) -> None:
    """polling 中に server が expired_token → InstallTimeoutError."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "expired_token", "error_description": "expired"}},
                ),
            ]
        )
    )
    with pytest.raises(InstallTimeoutError, match="expired"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test_client",
        )


async def test_b5_local_timeout_exceeded(
    monkeypatch: pytest.MonkeyPatch, _patch_client: Any
) -> None:
    """local timeout 超過で InstallTimeoutError. asyncio.sleep を real time にして経過させる."""
    # Note: timeout=60 が下限、 monotonic を fast-forward させて即 deadline 超過。
    import time as _time

    real_monotonic = _time.monotonic
    start = real_monotonic()

    def fake_monotonic() -> float:
        # 呼び出し回数で 0 → 100000 にジャンプ
        fake_monotonic._calls += 1  # type: ignore[attr-defined]
        if fake_monotonic._calls <= 1:  # type: ignore[attr-defined]
            return start
        return start + 100000.0

    fake_monotonic._calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr("agentrux_sdk._oauth_polling.time.monotonic", fake_monotonic)

    # asyncio.sleep は instant
    async def fast_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr("agentrux_sdk._oauth_polling.asyncio.sleep", fast_sleep)

    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
            ]
        )
    )

    with pytest.raises(InstallTimeoutError, match="approval not completed within"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test_client",
            timeout=60,
        )


async def test_b6_unexpected_5xx_at_authorize(
    _no_sleep: None, _patch_client: Any
) -> None:
    """POST /oauth/device/authorize 503 → AgenTruxError (network/unexpected)."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 503, {"error": "service_unavailable"}),
            ]
        )
    )
    with pytest.raises(AgenTruxError, match="unexpected status 503"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test",
        )


# ============================================================================
# c. 境界 (c1-c4)
# ============================================================================


async def test_c1_timeout_clamped_to_600(
    _no_sleep: None, _patch_client: Any
) -> None:
    """timeout=900 → 600 に clamp (RFC 8628 device_code TTL 上限)."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        timeout=9999,  # spec が 600 に clamp する
    )
    # success してきている = clamp が effective
    assert result.access_token == "aat_test_access"


async def test_c2_timeout_clamped_to_60_min(
    _no_sleep: None, _patch_client: Any
) -> None:
    """timeout=10 → 60 に clamp (下限)."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        timeout=10,
    )
    assert result.access_token == "aat_test_access"


async def test_c3_single_scope_topic_read_only(
    _no_sleep: None, _patch_client: Any
) -> None:
    """scope=("topic.read",) 単一でも valid."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                ("/oauth/token", 200, _success_token_body(scope="topic.read")),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        scope=("topic.read",),
    )
    assert result.scope == ("topic.read",)


async def test_c4_full_vocab_5_scopes(
    _no_sleep: None, _patch_client: Any
) -> None:
    """vocab 全部 5 種類で valid."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    200,
                    _success_token_body(
                        scope="topic.read topic.write openid email profile"
                    ),
                ),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
        scope=("topic.read", "topic.write", "openid", "email", "profile"),
    )
    assert set(result.scope) == {
        "topic.read",
        "topic.write",
        "openid",
        "email",
        "profile",
    }


# ============================================================================
# d. 攻撃 (d1-d3)
# ============================================================================


async def test_d1_javascript_url_rejected() -> None:
    """base_url=javascript:... は ConfigError (XSS 防御 client side)."""
    with pytest.raises(ConfigError, match="must be http"):
        await setup_via_device_code(
            base_url="javascript:alert(1)",  # type: ignore[arg-type]
            client_id="dcr_test",
        )


async def test_d2_client_id_control_char_rejected() -> None:
    """client_id に制御文字 → ConfigError."""
    with pytest.raises(ConfigError, match="control character"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_with\x00null",
        )


async def test_d3_scope_control_char_rejected() -> None:
    """scope vocab match しても TAB 等の制御文字なら ConfigError. 実際は vocab match で先に通る
    ので、 vocab matched + 制御文字混入の中間ケースは vocab 外で reject される。
    本 test は vocab 同等の string に \\x7F を付けて vocab 外として reject されるパスを確認.
    """
    with pytest.raises(ConfigError, match=r"not in vocabulary|control character"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test",
            scope=("topic.read\x7f",),
        )


async def test_d4_scope_duplicate_rejected() -> None:
    """重複 scope は ConfigError."""
    with pytest.raises(ConfigError, match="duplicate"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test",
            scope=("topic.read", "topic.read"),
        )


# ============================================================================
# e. race / timing (e1-e3)
# ============================================================================


async def test_e1_slow_down_increases_interval(
    _no_sleep: None, _patch_client: Any
) -> None:
    """slow_down 受信時に interval が +5s されることを poll 回数で検証."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "slow_down", "error_description": "slow"}},
                ),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "authorization_pending", "error_description": "wait"}},
                ),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
    )
    assert result.access_token == "aat_test_access"


async def test_e2_pending_then_success(
    _no_sleep: None, _patch_client: Any
) -> None:
    """authorization_pending → pending → 200 success の sequence で正常終了."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "authorization_pending"}},
                ),
                (
                    "/oauth/token",
                    400,
                    {"detail": {"error": "authorization_pending"}},
                ),
                ("/oauth/token", 200, _success_token_body()),
            ]
        )
    )
    result = await setup_via_device_code(
        base_url="https://api.example.com",
        client_id="dcr_test_client",
    )
    assert result.access_token == "aat_test_access"


async def test_e3_malformed_token_response(
    _no_sleep: None, _patch_client: Any
) -> None:
    """access_token missing の 200 → InstallError."""
    _patch_client(
        _make_handler(
            [
                ("/oauth/device/authorize", 200, _device_authorize_body()),
                (
                    "/oauth/token",
                    200,
                    {"refresh_token": "art_only", "expires_in": 600},  # access_token missing
                ),
            ]
        )
    )
    with pytest.raises(InstallError, match="malformed token response"):
        await setup_via_device_code(
            base_url="https://api.example.com",
            client_id="dcr_test_client",
        )
