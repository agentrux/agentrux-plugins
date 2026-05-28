"""Topology Request Flow v1 install helper.

SSOT: docs/04_design/auth/topology_request_v1.md (RFC 8628 + RFC 9396 RAR)

Public API:
    from agentrux.sdk.topology_install import (
        install_topology,
        TopologyDeclaration,
        TopologyTopicSpec,
        TopologyGrantSpec,
        InstallResult,
        InstallPendingInfo,
    )

Typical usage:
    result = await install_topology(
        base_url="https://api.agentrux.com",
        client_id="<oauth-public-client-id>",
        declaration=TopologyDeclaration(
            script_name="weather-bot",
            description="WeatherAPI を Composer に流す",
            topics=[
                TopologyTopicSpec(
                    ref="weather-data", name="weather-data",
                    retention_s=86400, intent="publish 1h interval",
                ),
            ],
            grants=[
                TopologyGrantSpec(
                    topic_ref="weather-data", scope="write",
                    binding_name="weather-out",
                ),
            ],
        ),
        on_user_code=lambda info: print(
            f"Visit {info.verification_uri_complete} (code: {info.user_code})"
        ),
    )
    print(result.access_token, result.topic_id_map)

設計判断 (sdk_design.md §1 と整合):
- I/O は httpx (既存 SDK と同 stack)
- network error は AgenTruxError, 仕様違反は InstallError に分離
- polling は asyncio.sleep based、 cancel 可
- on_user_code callback は **同期 / async どちらも許容** (asyncio.iscoroutine で分岐)
- result の access_token は短命 (≦600s)、 refresh_token (`art_`) で renew
"""

from __future__ import annotations

# asyncio は本 module 内で直接使わなくなったが、 既存 test が
# `monkeypatch.setattr("agentrux.sdk.topology_install.asyncio.sleep", ...)` の形で
# patch するため、 module attribute として残す (Python の `import asyncio` は global
# asyncio module を参照、 _oauth_polling.asyncio と同一 object なので patch は波及する)。
import asyncio  # noqa: F401 — back-compat for test monkeypatch
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

# Step 1a (device_code_setup_v1.md §5-1): polling / error parsing / Install* 階層を
# 共有 module に抽出。 public API は本 module 経由 re-export で不変保持。
from agentrux.sdk._oauth_errors import (
    InstallAbortedError,
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallTimeoutError,
)
from agentrux.sdk._oauth_errors import (
    parse_oauth_error as _parse_oauth_error,
)
from agentrux.sdk._oauth_polling import poll_device_token
from agentrux.sdk.errors import (
    AgenTruxError,
    ConfigError,
)

# RAR client-side validation constants (server側の SSOT と整合):
_VALID_SCOPES = frozenset({"read", "write"})
_MAX_TOPICS = 20
_MAX_GRANTS = 40
_MAX_DESCRIPTION = 256
_MAX_CLIENT_HINT = 256
_MAX_INTENT = 256
_MAX_RAR_BYTES = 16 * 1024
_BINDING_NAME_MIN = 1
_BINDING_NAME_MAX = 64
# DDL CHECK と一致: ^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$
_BINDING_NAME_RE = re.compile(r"^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$")


def _reject_control_chars(value: str, *, field: str) -> None:
    """\\x00-\\x1F + \\x7F を含む string は reject (server 側 _strip_validate_string 同等)."""
    for ch in value:
        c = ord(ch)
        if c < 0x20 or c == 0x7F:
            raise ConfigError(f"{field} contains control character")

# ----------------------------------------------------------------------------
# Input value objects
# ----------------------------------------------------------------------------

GrantScope = Literal["read", "write"]


@dataclass(frozen=True, slots=True)
class TopologyTopicSpec:
    """Request 内 topic 1 件 (RAR `authorization_details[0].topics[i]`)."""

    ref: str                # agent ↔ picker ↔ token 間の連結 key
    name: str               # topics.name 既存制約 ^[a-z0-9._-]{1,128}$
    retention_s: int        # [3600, 2592000] (topics.retention_ttl_seconds CHECK)
    intent: str | None = None  # picker 表示用


@dataclass(frozen=True, slots=True)
class TopologyGrantSpec:
    """Request 内 grant 1 件 (1 binding_name = 1 entry)."""

    topic_ref: str
    scope: GrantScope
    binding_name: str | None = None


@dataclass(frozen=True, slots=True)
class TopologyDeclaration:
    """Agent が宣言する topology の完全な input (RAR authorization_details v1)."""

    script_name: str
    description: str
    topics: tuple[TopologyTopicSpec, ...]
    grants: tuple[TopologyGrantSpec, ...]
    policy_match_inputs: dict[str, Any] | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if self.version != 1:
            raise ConfigError(f"unsupported topology version: {self.version}")
        if not self.script_name:
            raise ConfigError("script_name must be non-empty")
        if not self.description:
            raise ConfigError("description must be non-empty")
        # round-2 補完: server 側 size limit と整合 (Codex MF-1)
        if len(self.description) > _MAX_DESCRIPTION:
            raise ConfigError(
                f"description exceeds {_MAX_DESCRIPTION} chars"
            )
        _reject_control_chars(self.script_name, field="script_name")
        _reject_control_chars(self.description, field="description")
        if not self.topics:
            raise ConfigError("at least 1 topic required")
        if len(self.topics) > _MAX_TOPICS:
            raise ConfigError(f"topics exceeds limit {_MAX_TOPICS}")
        if not self.grants:
            raise ConfigError("at least 1 grant required")
        if len(self.grants) > _MAX_GRANTS:
            raise ConfigError(f"grants exceeds limit {_MAX_GRANTS}")
        topic_refs = {t.ref for t in self.topics}
        # topic level checks
        for i, t in enumerate(self.topics):
            if t.intent is not None and len(t.intent) > _MAX_INTENT:
                raise ConfigError(
                    f"topics[{i}].intent exceeds {_MAX_INTENT} chars"
                )
            if t.intent is not None:
                _reject_control_chars(t.intent, field=f"topics[{i}].intent")
            _reject_control_chars(t.ref, field=f"topics[{i}].ref")
            _reject_control_chars(t.name, field=f"topics[{i}].name")
        # 内部 schema 検証 (server も同様の check するが、 早期 fail で UX 向上)
        seen_bindings: set[str] = set()
        seen_topic_scope: set[tuple[str, str]] = set()
        for i, g in enumerate(self.grants):
            if g.scope not in _VALID_SCOPES:
                raise ConfigError(
                    f"grants[{i}].scope={g.scope!r} must be 'read' or 'write'"
                )
            if g.topic_ref not in topic_refs:
                raise ConfigError(
                    f"grants[{i}].topic_ref={g.topic_ref!r} not in topics"
                )
            if (g.topic_ref, g.scope) in seen_topic_scope:
                raise ConfigError(
                    f"grants[{i}] duplicate (topic_ref, scope) entry"
                )
            seen_topic_scope.add((g.topic_ref, g.scope))
            if g.binding_name is not None:
                if not (_BINDING_NAME_MIN <= len(g.binding_name) <= _BINDING_NAME_MAX):
                    raise ConfigError(
                        f"grants[{i}].binding_name length must be in "
                        f"[{_BINDING_NAME_MIN}, {_BINDING_NAME_MAX}]"
                    )
                if not _BINDING_NAME_RE.match(g.binding_name):
                    raise ConfigError(
                        f"grants[{i}].binding_name {g.binding_name!r} must match "
                        r"^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$ (no leading/trailing whitespace)"
                    )
                if g.binding_name in seen_bindings:
                    raise ConfigError(
                        f"grants[{i}].binding_name={g.binding_name!r} duplicate"
                    )
                seen_bindings.add(g.binding_name)
        # full RAR payload size check (16KB) — server 側 _MAX_RAR_BYTES と一致
        rar_json = self.to_authorization_details_json()
        if len(rar_json.encode("utf-8")) > _MAX_RAR_BYTES:
            raise ConfigError(
                f"authorization_details exceeds {_MAX_RAR_BYTES} bytes"
            )

    def to_authorization_details_json(self) -> str:
        """RFC 9396 `authorization_details` 1 entry の JSON 文字列."""
        return json.dumps(
            [
                {
                    "type": "agentrux.topology",
                    "version": self.version,
                    "script": {
                        "name": self.script_name,
                        "description": self.description,
                    },
                    "topics": [
                        {
                            "ref": t.ref,
                            "name": t.name,
                            "retention_s": t.retention_s,
                            "intent": t.intent,
                        }
                        for t in self.topics
                    ],
                    "grants": [
                        {
                            "topic_ref": g.topic_ref,
                            "scope": g.scope,
                            "binding_name": g.binding_name,
                        }
                        for g in self.grants
                    ],
                    "policy_match_inputs": self.policy_match_inputs,
                }
            ],
            ensure_ascii=False,
        )


# ----------------------------------------------------------------------------
# Output value objects
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstallPendingInfo:
    """user に表示すべき情報 (on_user_code callback の input)."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class InstallResultGrant:
    """approve 完了で発行された grant エントリ."""

    topic_scope_key: str         # "topic:top_<uuid>:<scope>" (JWT scope と同形式)
    grant_id: str                # "grt_<uuid>"
    binding_name: str | None


@dataclass(frozen=True, slots=True)
class InstallResult:
    """install_topology の戻り値. agent が runtime で保持する all-in-one credential bundle."""

    access_token: str            # aat_<JWT>
    refresh_token: str           # art_<opaque>
    expires_in: int              # access_token TTL (seconds)
    scope: tuple[str, ...]       # space-delimited scope を split したもの
    script_id: str               # scr_<uuid>
    alias_id: str                # ali_<uuid>
    topic_id_map: dict[str, str]  # ref → top_<uuid>
    grants: tuple[InstallResultGrant, ...]
    granted_at_unix: float       # 受領時刻 (refresh_lead 計算用)

    def topic_id(self, ref: str) -> str:
        """ref から実 topic_id を引く. ref が無ければ KeyError."""
        return self.topic_id_map[ref]


# ----------------------------------------------------------------------------
# Error types — Step 1a で _oauth_errors.py に集約済、 ここでは re-export のみ
# (既存 caller の `from agentrux.sdk.topology_install import InstallError` を維持)
# ----------------------------------------------------------------------------

# InstallError / InstallDeniedError / InstallTimeoutError / InstallAuthError /
# InstallAbortedError は module top-level の import で取り込み済 + __all__ で公開。


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

_TOPOLOGY_REQUEST_PATH = "/oauth/topology-request"
_USER_AGENT = "agentrux-sdk-topology-install/1.0"
_DEFAULT_TIMEOUT_S = 600


OnUserCodeCallback = Callable[[InstallPendingInfo], None | Awaitable[None]]


# ----------------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------------


async def install_topology(
    *,
    base_url: str,
    client_id: str,
    declaration: TopologyDeclaration,
    on_user_code: OnUserCodeCallback,
    timeout: int = _DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (caller-friendly seconds, not asyncio.timeout)
    client_hint: str | None = None,
    connect_timeout: float = 5.0,
    read_timeout: float = 30.0,
) -> InstallResult:
    """Topology Request Flow v1 install (SSOT §1).

    Args:
        base_url: AgenTrux API base (例: "https://api.agentrux.com")
        client_id: registered OAuth public client UUID (DCR or pre-registered)
        declaration: 申請する topology
        on_user_code: user_code + verification_uri を operator に表示する callback
            (同期 / async どちらも可、 同期は内部で await されない)
        timeout: 全体 timeout (≤ device_code TTL = 600s 既存制約に丸める)
        client_hint: picker UI に表示する任意 string (≤256 chars)
        connect_timeout / read_timeout: httpx timeouts

    Returns:
        InstallResult (aat_/art_ + topology bindings)

    Raises:
        ConfigError: input 不正 (client side detected)
        AuthenticationError: client_id 不明 / OAuth client 認証不可
        InstallError / InstallDeniedError / InstallTimeoutError:
            user 拒否 / timeout / RAR schema 違反 / その他 OAuth エラー
        AgenTruxError: network / 想定外 status
    """
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"base_url must be http(s) URL: {base_url!r}")
    if not client_id:
        raise ConfigError("client_id is required")
    base = base_url.rstrip("/")

    timeout = max(60, min(timeout, _DEFAULT_TIMEOUT_S))

    async with httpx.AsyncClient(
        base_url=base,
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=read_timeout),
        headers={"User-Agent": _USER_AGENT},
    ) as http:
        # 1. issue topology-request (= device_code)
        device_code, pending = await _issue_topology_request(
            http=http,
            client_id=client_id,
            declaration=declaration,
            client_hint=client_hint,
        )

        # 2. notify operator
        cb_ret = on_user_code(pending)
        if inspect.isawaitable(cb_ret):
            await cb_ret  # type: ignore[arg-type]

        # 3. poll /oauth/token
        return await _poll_token(
            http=http,
            client_id=client_id,
            pending=pending,
            timeout_s=timeout,
            device_code=device_code,
        )


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


async def _issue_topology_request(
    *,
    http: httpx.AsyncClient,
    client_id: str,
    declaration: TopologyDeclaration,
    client_hint: str | None,
) -> tuple[str, InstallPendingInfo]:
    """POST /oauth/topology-request → (device_code, public pending info).

    device_code は polling 内部で使う bearer。 on_user_code には流さない (raw bearer
    の意図せぬログ流出を避ける)。
    """
    data: dict[str, str] = {
        "client_id": client_id,
        "authorization_details": declaration.to_authorization_details_json(),
    }
    if client_hint is not None:
        data["client_hint"] = client_hint
    try:
        r = await http.post(
            _TOPOLOGY_REQUEST_PATH,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise AgenTruxError(f"network error during topology-request: {exc}") from exc

    if r.status_code == 200:
        body = r.json()
        return str(body["device_code"]), InstallPendingInfo(
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=str(body["verification_uri_complete"]),
            expires_in=int(body["expires_in"]),
            interval=int(body["interval"]),
        )

    # error mapping
    code, desc = _parse_oauth_error(r)
    if r.status_code == 400 and code == "invalid_client":
        raise InstallAuthError(f"invalid_client: {desc}")
    if r.status_code == 400 and code.startswith("unsupported_authorization_details"):
        raise InstallError(f"{code}: {desc}")
    if r.status_code == 400:
        raise InstallError(f"{code or 'invalid_request'}: {desc}")
    if r.status_code == 429:
        raise InstallError(f"rate_limited: {desc}")
    raise AgenTruxError(
        f"unexpected status {r.status_code} from {_TOPOLOGY_REQUEST_PATH}: {desc}"
    )


async def _poll_token(
    *,
    http: httpx.AsyncClient,
    client_id: str,
    pending: InstallPendingInfo,
    timeout_s: int,
    device_code: str,
) -> InstallResult:
    """RFC 8628 §3.4 polling loop。 Step 1a で polling 部分は `poll_device_token()` に
    抽出済 (挙動不変)。 本 wrapper は topology 固有の result parse のみ実施。
    """
    body = await poll_device_token(
        http=http,
        client_id=client_id,
        device_code=device_code,
        user_code=pending.user_code,
        timeout_s=timeout_s,
        initial_interval=float(pending.interval),
    )
    return _parse_token_response(body)


def _parse_token_response(body: dict[str, Any]) -> InstallResult:
    """200 OK token response → InstallResult."""
    try:
        access_token = str(body["access_token"])
        refresh_token = str(body["refresh_token"])
        expires_in = int(body["expires_in"])
    except (KeyError, ValueError) as exc:
        raise AgenTruxError(f"malformed token response: missing field {exc}") from exc

    scope = tuple(str(body.get("scope", "")).split())

    # RFC 9396 authorization_details (topology v1)
    ad = body.get("authorization_details")
    if not isinstance(ad, list) or not ad:
        raise InstallError(
            "token response missing authorization_details "
            "(this should not happen for a topology-request)"
        )
    granted = ad[0].get("granted")
    if not isinstance(granted, dict):
        raise InstallError("authorization_details[0].granted missing")

    try:
        script_id = str(granted["script_id"])
        alias_id = str(granted["alias_id"])
        topic_id_map_raw = granted.get("topic_id_map") or {}
        if not isinstance(topic_id_map_raw, dict):
            raise InstallError("topic_id_map must be object")
        topic_id_map = {str(k): str(v) for k, v in topic_id_map_raw.items()}

        grant_ids_raw = granted.get("grant_ids") or {}
        if not isinstance(grant_ids_raw, dict):
            raise InstallError("grant_ids must be object")
        # Codex MF-3: malformed エントリを silent skip せず InstallError で reject。
        grants: list[InstallResultGrant] = []
        for key, val in grant_ids_raw.items():
            if not isinstance(val, dict):
                raise InstallError(
                    f"grant_ids[{key!r}] must be object, got {type(val).__name__}"
                )
            grant_id_raw = val.get("grant_id")
            if not isinstance(grant_id_raw, str) or not grant_id_raw:
                raise InstallError(
                    f"grant_ids[{key!r}].grant_id must be non-empty string"
                )
            bn = val.get("binding_name")
            if bn is not None and not isinstance(bn, str):
                raise InstallError(
                    f"grant_ids[{key!r}].binding_name must be string or null"
                )
            grants.append(
                InstallResultGrant(
                    topic_scope_key=str(key),
                    grant_id=grant_id_raw,
                    binding_name=bn,
                )
            )
    except KeyError as exc:
        raise InstallError(f"authorization_details missing field: {exc}") from exc

    return InstallResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scope=scope,
        script_id=script_id,
        alias_id=alias_id,
        topic_id_map=topic_id_map,
        grants=tuple(grants),
        granted_at_unix=time.time(),
    )


# _parse_oauth_error は Step 1a で _oauth_errors.parse_oauth_error() に移動済、
# 本 module は import 経由で alias 再 export している (上記 import block 参照)。


__all__ = [
    "InstallAbortedError",
    "InstallAuthError",
    "InstallDeniedError",
    "InstallError",
    "InstallPendingInfo",
    "InstallResult",
    "InstallResultGrant",
    "InstallTimeoutError",
    "OnUserCodeCallback",
    "TopologyDeclaration",
    "TopologyGrantSpec",
    "TopologyTopicSpec",
    "install_topology",
]
