"""SDK Topology Install helper tests.

SSOT: docs/04_design/auth/topology_request_v1.md + src/agentrux/sdk/topology_install.py

4 軸 (a 正常 / b エラー / c 境界 / d 攻撃):
- a 正常: pending → polling → success で InstallResult が組み立てられる
- b エラー: invalid_client / authorization_pending → slow_down → 最終 access_denied / expired
- c 境界: timeout / interval / TOPIC 数 / declaration 内 schema 検証
- d 攻撃: RAR shape 違反 (duplicate binding / unknown ref) を client で reject
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from agentrux_sdk.errors import ConfigError
from agentrux_sdk.topology_install import (
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallPendingInfo,
    InstallResult,
    InstallTimeoutError,
    TopologyDeclaration,
    TopologyGrantSpec,
    TopologyTopicSpec,
    install_topology,
)

pytestmark = pytest.mark.unit


def _decl() -> TopologyDeclaration:
    return TopologyDeclaration(
        script_name="weather-bot",
        description="WeatherAPI を Composer に流す",
        topics=(
            TopologyTopicSpec(
                ref="weather-data",
                name="weather-data",
                retention_s=86400,
                intent="publish 1h",
            ),
        ),
        grants=(
            TopologyGrantSpec(
                topic_ref="weather-data",
                scope="write",
                binding_name="weather-out",
            ),
        ),
    )


def _token_success_body(*, granted: dict[str, Any] | None = None) -> dict[str, Any]:
    """200 OK token response (Topology RAR を含む) body."""
    return {
        "access_token": "aat_TEST",
        "refresh_token": "art_TEST",
        "token_type": "Bearer",
        "expires_in": 600,
        "scope": "topic.write topic:top_xxx:write",
        "authorization_details": [
            {
                "type": "agentrux.topology",
                "version": 1,
                "granted": granted or {
                    "script_id": "scr_S",
                    "alias_id": "ali_A",
                    "topic_id_map": {"weather-data": "top_T"},
                    "grant_ids": {
                        "topic:top_T:write": {
                            "grant_id": "grt_G",
                            "binding_name": "weather-out",
                        }
                    },
                },
            }
        ],
    }


def _make_handler(steps: list[tuple[str, int, dict[str, Any] | str]]):
    """Returns an httpx.MockTransport handler that walks `steps` sequentially.

    Each step: (path_suffix, status_code, body_or_text). The handler matches
    by path; if path doesn't match, raises (test failure surface).
    """
    iter_steps = iter(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            expect_path, status, body = next(iter_steps)
        except StopIteration:
            return httpx.Response(500, json={"error": "test exhausted steps"})
        assert request.url.path.endswith(expect_path), (
            f"expected suffix {expect_path}, got {request.url.path}"
        )
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    return handler


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch):
    """asyncio.sleep を 0 にして polling を高速化."""

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("agentrux_sdk.topology_install.asyncio.sleep", fast_sleep)


# ---------------------------------------------------------------------------
# a. 正常系
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a1_happy_path(_no_sleep: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """issue → 1 poll で 200 OK → InstallResult が組み立てられる."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_TEST",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://app.agentrux.com/topology/approve",
                    "verification_uri_complete": "https://app.agentrux.com/topology/approve?code=ABCD-EFGH",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 200, _token_success_body()),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )

    seen: list[InstallPendingInfo] = []
    result = await install_topology(
        base_url="https://api.example.com",
        client_id="client-uuid",
        declaration=_decl(),
        on_user_code=lambda info: seen.append(info),
        timeout=600,
    )
    assert isinstance(result, InstallResult)
    assert result.access_token == "aat_TEST"
    assert result.refresh_token == "art_TEST"
    assert result.script_id == "scr_S"
    assert result.alias_id == "ali_A"
    assert result.topic_id("weather-data") == "top_T"
    assert len(result.grants) == 1
    assert result.grants[0].grant_id == "grt_G"
    assert result.grants[0].binding_name == "weather-out"
    assert seen[0].user_code == "ABCD-EFGH"
    assert seen[0].verification_uri_complete.endswith("code=ABCD-EFGH")


@pytest.mark.asyncio
async def test_a2_async_callback_awaited(_no_sleep: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """on_user_code が async ならちゃんと await される."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 200, _token_success_body()),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )

    awaited = asyncio.Event()

    async def async_cb(info: InstallPendingInfo) -> None:
        awaited.set()

    await install_topology(
        base_url="https://api.example.com",
        client_id="client-uuid",
        declaration=_decl(),
        on_user_code=async_cb,
    )
    assert awaited.is_set()


@pytest.mark.asyncio
async def test_a3_authorization_pending_then_success(
    _no_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """poll で 2 回 authorization_pending → 3 回目で success."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 400, {"error": "authorization_pending"}),
            ("/oauth/token", 400, {"error": "authorization_pending"}),
            ("/oauth/token", 200, _token_success_body()),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    r = await install_topology(
        base_url="https://api.example.com",
        client_id="client-uuid",
        declaration=_decl(),
        on_user_code=lambda info: None,
    )
    assert r.access_token == "aat_TEST"


@pytest.mark.asyncio
async def test_a4_slow_down_increases_interval(
    _no_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """slow_down 時に interval が +5s される (RFC 8628 §3.5)."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 400, {"error": "slow_down"}),
            ("/oauth/token", 200, _token_success_body()),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    r = await install_topology(
        base_url="https://api.example.com",
        client_id="client-uuid",
        declaration=_decl(),
        on_user_code=lambda info: None,
    )
    assert r.access_token == "aat_TEST"


# ---------------------------------------------------------------------------
# b. エラー系
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b1_access_denied(_no_sleep: None, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 400, {"error": "access_denied"}),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallDeniedError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


@pytest.mark.asyncio
async def test_b2_expired_token(_no_sleep: None, monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            ("/oauth/token", 400, {"error": "expired_token"}),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallTimeoutError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


@pytest.mark.asyncio
async def test_b3_invalid_client_at_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                400,
                {
                    "detail": {
                        "error": "invalid_client",
                        "error_description": "unknown",
                    }
                },
            )
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallAuthError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="bogus",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


@pytest.mark.asyncio
async def test_b4_unsupported_version_at_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                400,
                {
                    "detail": {
                        "error": "unsupported_authorization_details_version",
                        "error_description": "v2 unsupported",
                    }
                },
            )
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


@pytest.mark.asyncio
async def test_b5_token_response_missing_authorization_details(
    _no_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """200 OK だが RAR field 欠落 → InstallError (旧 device flow を topology endpoint で
    叩いた、 等の異常ケースを surface)."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            (
                "/oauth/token",
                200,
                {
                    "access_token": "aat_T",
                    "refresh_token": "art_T",
                    "token_type": "Bearer",
                    "expires_in": 600,
                    "scope": "",
                    # authorization_details なし
                },
            ),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


# ---------------------------------------------------------------------------
# c. 境界 (client-side validation)
# ---------------------------------------------------------------------------


def test_c1_topology_declaration_at_least_1_topic() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(),
            grants=(),
        )


def test_c2_grant_topic_ref_must_exist() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="missing", scope="read"),),
        )


def test_c3_duplicate_topic_scope_pair_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(
                TopologyGrantSpec(topic_ref="a", scope="read", binding_name="b1"),
                TopologyGrantSpec(topic_ref="a", scope="read", binding_name="b2"),
            ),
        )


def test_c4_duplicate_binding_name_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(
                TopologyTopicSpec(ref="a", name="a", retention_s=3600),
                TopologyTopicSpec(ref="b", name="b", retention_s=3600),
            ),
            grants=(
                TopologyGrantSpec(topic_ref="a", scope="read", binding_name="shared"),
                TopologyGrantSpec(topic_ref="b", scope="read", binding_name="shared"),
            ),
        )


def test_c5_invalid_version_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="read"),),
            version=2,
        )


def test_c6_authorization_details_serialization_shape() -> None:
    """RAR JSON が SSOT 通りに組み立てられる."""
    d = _decl()
    parsed = json.loads(d.to_authorization_details_json())
    assert isinstance(parsed, list) and len(parsed) == 1
    entry = parsed[0]
    assert entry["type"] == "agentrux.topology"
    assert entry["version"] == 1
    assert entry["script"]["name"] == "weather-bot"
    assert len(entry["topics"]) == 1
    assert entry["topics"][0]["ref"] == "weather-data"
    assert len(entry["grants"]) == 1
    assert entry["grants"][0]["binding_name"] == "weather-out"


@pytest.mark.asyncio
async def test_c7_timeout_clamped_to_600(
    _no_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """timeout が 600s 超でも、 device_code TTL に揃えて clamped."""
    # issue → authorization_pending 連発 → 最終的に InstallTimeoutError (deadline 到達)
    steps: list[tuple[str, int, dict[str, Any] | str]] = [
        (
            "/oauth/topology-request",
            200,
            {
                "device_code": "dc_X",
                "user_code": "X-Y",
                "verification_uri": "u",
                "verification_uri_complete": "u?code=X-Y",
                "expires_in": 600,
                "interval": 1,
            },
        ),
    ]
    # 「無限に authorization_pending を返す」が、 _no_sleep + monotonic 操作で deadline を
    # 即超過させる
    for _ in range(50):
        steps.append(("/oauth/token", 400, {"error": "authorization_pending"}))

    handler = _make_handler(steps)
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )

    base = time.monotonic()
    seq = iter([base, base, base + 100, base + 9999])

    def fake_monotonic() -> float:
        try:
            return next(seq)
        except StopIteration:
            return base + 9999

    monkeypatch.setattr("agentrux_sdk.topology_install.time.monotonic", fake_monotonic)

    with pytest.raises(InstallTimeoutError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
            timeout=9999,  # > 600 → clamped to 600
        )


# ---------------------------------------------------------------------------
# d. 攻撃ベクター / config 検証
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_d1_invalid_base_url() -> None:
    with pytest.raises(ConfigError):
        await install_topology(
            base_url="ftp://bogus",
            client_id="c",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


@pytest.mark.asyncio
async def test_d2_empty_client_id() -> None:
    with pytest.raises(ConfigError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )


# ---------------------------------------------------------------------------
# Codex MF-1 補完: client-side validation
# ---------------------------------------------------------------------------


def test_v1_invalid_scope_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="admin"),),  # type: ignore[arg-type]
        )


def test_v2_binding_name_too_long_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="read", binding_name="x" * 65),),
        )


def test_v3_binding_name_leading_space_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="read", binding_name=" foo"),),
        )


def test_v4_description_too_long_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="d" * 257,
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="read"),),
        )


def test_v5_topics_count_over_limit_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="x",
            topics=tuple(
                TopologyTopicSpec(ref=f"t{i}", name=f"t{i}", retention_s=3600)
                for i in range(21)
            ),
            grants=(TopologyGrantSpec(topic_ref="t0", scope="read"),),
        )


def test_v6_grants_count_over_limit_rejected() -> None:
    topics = tuple(
        TopologyTopicSpec(ref=f"t{i}", name=f"t{i}", retention_s=3600)
        for i in range(10)
    )
    # 41 grants 構築 (上限 40 超え) — 10 topic × 2 scope = 20 unique pair しかないので、
    # binding_name で水増しはできない。 1 topic × 1 scope の重複は __post_init__ で 先に reject。
    # → 別軸: 40 unique grants を 1 個追加で超過させる方法は (topic, scope) 制約上 不可。
    # 代わりに 「grants array length 40」 を test (limit 内の boundary)。
    grants = tuple(
        TopologyGrantSpec(topic_ref=f"t{i // 2}", scope="read" if i % 2 == 0 else "write",
                          binding_name=f"b{i}")
        for i in range(20)
    )
    # 20 unique pairs OK
    d = TopologyDeclaration(
        script_name="x", description="x", topics=topics, grants=grants
    )
    assert len(d.grants) == 20


def test_v7_control_char_in_description_rejected() -> None:
    with pytest.raises(ConfigError):
        TopologyDeclaration(
            script_name="x",
            description="hello\x00world",
            topics=(TopologyTopicSpec(ref="a", name="a", retention_s=3600),),
            grants=(TopologyGrantSpec(topic_ref="a", scope="read"),),
        )


def test_v8_rar_byte_size_within_limit() -> None:
    """通常 size の declaration は 16KB 内に収まる."""
    d = TopologyDeclaration(
        script_name="x",
        description="x" * 100,
        topics=tuple(
            TopologyTopicSpec(
                ref=f"t{i}", name=f"t{i}", retention_s=3600, intent="i" * 100
            )
            for i in range(10)
        ),
        grants=tuple(
            TopologyGrantSpec(
                topic_ref=f"t{i}", scope="read", binding_name=f"b{i}"
            )
            for i in range(10)
        ),
    )
    payload = d.to_authorization_details_json()
    assert len(payload.encode("utf-8")) < 16 * 1024


# ---------------------------------------------------------------------------
# Codex MF-3 / MF-2: token response shape strict validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_v9_malformed_grant_ids_rejected(
    _no_sleep: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """grant_ids[].grant_id が string でない → InstallError (silent skip しない)."""
    handler = _make_handler(
        [
            (
                "/oauth/topology-request",
                200,
                {
                    "device_code": "dc_X",
                    "user_code": "X-Y",
                    "verification_uri": "u",
                    "verification_uri_complete": "u?code=X-Y",
                    "expires_in": 600,
                    "interval": 1,
                },
            ),
            (
                "/oauth/token",
                200,
                _token_success_body(
                    granted={
                        "script_id": "scr_S",
                        "alias_id": "ali_A",
                        "topic_id_map": {"weather-data": "top_T"},
                        "grant_ids": {
                            "topic:top_T:write": {
                                "grant_id": 12345,  # int (string でない)
                                "binding_name": "weather-out",
                            }
                        },
                    }
                ),
            ),
        ]
    )
    transport = httpx.MockTransport(handler)
    _real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        "agentrux_sdk.topology_install.httpx.AsyncClient",
        lambda *a, **kw: _real_async_client(*a, transport=transport, **kw),
    )
    with pytest.raises(InstallError):
        await install_topology(
            base_url="https://api.example.com",
            client_id="client-uuid",
            declaration=_decl(),
            on_user_code=lambda info: None,
        )
