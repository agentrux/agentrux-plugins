"""Plain Device Code (RFC 8628) plugin setup helper — no RAR.

SSOT: docs/04_design/auth/device_code_setup_v1.md §3-1

`install_topology()` (RAR 拡張版) と並列に、 **RAR なし** の単純な device code 経由
credential 取得を SDK で提供する。 backend は既存 RFC 8628 endpoint (`POST /oauth/device/authorize`
+ `POST /device/verify` + `POST /oauth/token grant_type=device_code`) をそのまま reuse。

Public API:
    from agentrux.sdk.device_code_setup import (
        setup_via_device_code,
        DeviceCodeSetupResult,
        DeviceCodeSetupPending,
    )

Typical usage:
    result = await setup_via_device_code(
        base_url="https://api.agentrux.com",
        client_id="<dcr_client_id>",
        scope=("topic.read", "topic.write"),
        on_user_code=lambda info: print(f"Visit {info.verification_uri_complete}"),
    )
    print(result.access_token[:20], result.scope)

設計判断 (device_code_setup_v1.md §0-4):
- error 階層は既存 `InstallError` 系を **再利用** (新規 SetupError 階層不採用)
- scope vocab pre-validation (`topic.read|topic.write|openid|email|profile` のみ)
- `id_token` は openid scope 時のみ optional に含む
- polling は `_oauth_polling.poll_device_token()` を共有
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

# Re-export Install* hierarchy (caller convenience、 device_code_setup_v1.md §3-1)
from agentrux.sdk._oauth_errors import (
    InstallAbortedError,
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallTimeoutError,
    parse_oauth_error,
)
from agentrux.sdk._oauth_errors import (
    InstallAuthError as _InstallAuthError,  # noqa: F401 re-export marker
)
from agentrux.sdk._oauth_errors import (
    InstallError as _InstallError,  # noqa: F401 re-export marker
)
from agentrux.sdk._oauth_polling import poll_device_token
from agentrux.sdk.errors import AgenTruxError, ConfigError

# Scope vocabulary (server `is_valid_authorize_scope()` と整合、 device_code_setup_v1.md §0-4)
_VALID_SCOPE_VOCAB: frozenset[str] = frozenset(
    {"topic.read", "topic.write", "openid", "email", "profile"}
)

_DEVICE_AUTHORIZE_PATH = "/oauth/device/authorize"
_USER_AGENT = "agentrux-sdk-device-code-setup/1.0"
_DEFAULT_TIMEOUT_S = 600
_MIN_TIMEOUT_S = 60


@dataclass(frozen=True, slots=True)
class DeviceCodeSetupPending:
    """`on_user_code` callback に渡る public 情報 (raw device_code は流さない)."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class DeviceCodeSetupResult:
    """`setup_via_device_code()` の戻り値. Topology Flow `InstallResult` と shape は近いが、
    `authorization_details` 由来の topic_id_map / grants は含まない (plain device code は
    setup 時点で resource を作らないため)."""

    access_token: str                    # "aat_..."
    refresh_token: str | None            # "art_..." (public client なら必ず付く)
    scope: tuple[str, ...]               # granted scope vocabulary
    expires_in: int                      # access_token の TTL 秒
    id_token: str | None = None          # openid scope 指定時のみ
    granted_scopes: tuple[str, ...] = field(default_factory=tuple)  # scope alias (legacy compat)
    granted_at_unix: float = 0.0         # 受領時刻 (refresh_lead 計算用)


# callback can be sync or async
OnUserCodeCallback = Callable[[DeviceCodeSetupPending], None | Awaitable[None]]


def _validate_scope(scope: Sequence[str]) -> None:
    """Scope vocab 違反 / 重複 / 空 を ConfigError で reject (server に投げる前に early fail)."""
    if not scope:
        raise ConfigError("scope must be non-empty")
    seen: set[str] = set()
    for s in scope:
        if not isinstance(s, str) or not s:
            raise ConfigError(f"scope entry must be non-empty string: {s!r}")
        if s in seen:
            raise ConfigError(f"scope duplicate: {s!r}")
        seen.add(s)
        if s not in _VALID_SCOPE_VOCAB:
            raise ConfigError(
                f"scope {s!r} not in vocabulary "
                f"({sorted(_VALID_SCOPE_VOCAB)})"
            )
        # control char reject (server `_strip_validate_string` と整合)
        for ch in s:
            c = ord(ch)
            if c < 0x20 or c == 0x7F:
                raise ConfigError(f"scope {s!r} contains control character")


async def setup_via_device_code(
    *,
    base_url: str,
    client_id: str,
    scope: Sequence[str] = ("topic.read", "topic.write"),
    on_user_code: OnUserCodeCallback | None = None,
    timeout: int = _DEFAULT_TIMEOUT_S,  # noqa: ASYNC109 (caller-friendly seconds, not asyncio.timeout)
    connect_timeout: float = 5.0,
    read_timeout: float = 30.0,
    http: httpx.AsyncClient | None = None,
) -> DeviceCodeSetupResult:
    """RFC 8628 Device Authorization Grant (RAR なし) で credential を取得.

    Args:
        base_url: AgenTrux API base URL (例: "https://api.agentrux.com")。 trailing slash 許容。
        client_id: prior DCR で取得した OAuth public client UUID。
        scope: scope vocabulary (topic.read|topic.write|openid|email|profile の subset)。
            default は ("topic.read", "topic.write")。
        on_user_code: callback。 引数は DeviceCodeSetupPending。 同期 / async どちらも可。
        timeout: 全体 deadline 秒 (range [60, 600]、 device_code TTL ≤600s 制約)。
        connect_timeout / read_timeout: httpx timeouts。
        http: optional 共有 httpx.AsyncClient (caller 側で reuse する場合)。

    Returns:
        DeviceCodeSetupResult (aat_ + art_ + optional id_token)。

    Raises:
        ConfigError: input 不正 (client side detected: base_url / client_id / scope)。
        InstallAuthError: client_id 不正 / scope vocab 違反 / invalid_grant 系。
        InstallDeniedError: user が picker で deny。
        InstallTimeoutError: timeout / device_code expired。
        InstallAbortedError: asyncio.CancelledError catch (caller 側で raise する想定)。
        InstallError: その他致命的エラー。
        AgenTruxError: network エラー / 想定外 status。
    """
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ConfigError(f"base_url must be http(s) URL: {base_url!r}")
    if not isinstance(client_id, str) or not client_id:
        raise ConfigError("client_id is required")
    # control char reject (XSS like javascript: は startswith check で reject 済、
    # ここでは client_id の URL-unsafe char を guard)
    for ch in client_id:
        c = ord(ch)
        if c < 0x20 or c == 0x7F:
            raise ConfigError("client_id contains control character")
    _validate_scope(scope)

    base = base_url.rstrip("/")
    timeout_clamped = max(_MIN_TIMEOUT_S, min(int(timeout), _DEFAULT_TIMEOUT_S))

    if http is not None:
        # caller-provided client: do NOT close it
        return await _setup_with_client(
            http=http,
            base=base,
            client_id=client_id,
            scope=tuple(scope),
            on_user_code=on_user_code,
            timeout_s=timeout_clamped,
        )

    async with httpx.AsyncClient(
        base_url=base,
        timeout=httpx.Timeout(
            connect=connect_timeout, read=read_timeout, write=read_timeout, pool=read_timeout
        ),
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        return await _setup_with_client(
            http=client,
            base=base,
            client_id=client_id,
            scope=tuple(scope),
            on_user_code=on_user_code,
            timeout_s=timeout_clamped,
        )


async def _setup_with_client(
    *,
    http: httpx.AsyncClient,
    base: str,
    client_id: str,
    scope: tuple[str, ...],
    on_user_code: OnUserCodeCallback | None,
    timeout_s: int,
) -> DeviceCodeSetupResult:
    """共有 httpx client を使った内部実装。 base_url 設定済前提."""
    # 1. issue device_code (POST /oauth/device/authorize)
    device_code, pending = await _issue_device_code(
        http=http, client_id=client_id, scope=scope
    )

    # 2. notify operator (sync / async callable 両対応)
    if on_user_code is not None:
        cb_ret = on_user_code(pending)
        if inspect.isawaitable(cb_ret):
            await cb_ret  # type: ignore[arg-type]

    # 3. poll /oauth/token (RFC 8628 §3.4、 _oauth_polling 経由)
    body = await poll_device_token(
        http=http,
        client_id=client_id,
        device_code=device_code,
        user_code=pending.user_code,
        timeout_s=timeout_s,
        initial_interval=float(pending.interval),
    )
    return _parse_token_response(body)


async def _issue_device_code(
    *,
    http: httpx.AsyncClient,
    client_id: str,
    scope: tuple[str, ...],
) -> tuple[str, DeviceCodeSetupPending]:
    """POST /oauth/device/authorize → (device_code, public pending info).

    device_code は polling 内部で使う bearer、 on_user_code には流さない (raw bearer の
    意図せぬログ流出を回避、 spec §3 設計判断)。
    """
    data = {
        "client_id": client_id,
        "scope": " ".join(scope),
    }
    try:
        r = await http.post(
            _DEVICE_AUTHORIZE_PATH,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        raise AgenTruxError(f"network error during device authorize: {exc}") from exc

    if r.status_code == 200:
        body = r.json()
        return str(body["device_code"]), DeviceCodeSetupPending(
            user_code=str(body["user_code"]),
            verification_uri=str(body["verification_uri"]),
            verification_uri_complete=str(body["verification_uri_complete"]),
            expires_in=int(body["expires_in"]),
            interval=int(body["interval"]),
        )

    code, desc = parse_oauth_error(r)
    if r.status_code == 400 and code == "invalid_client":
        raise InstallAuthError(f"invalid_client: {desc}")
    if r.status_code == 400 and code == "invalid_scope":
        raise InstallAuthError(f"invalid_scope: {desc}")
    if r.status_code == 400:
        raise InstallError(f"{code or 'invalid_request'}: {desc}")
    if r.status_code == 429:
        raise InstallError(f"rate_limited: {desc}")
    raise AgenTruxError(
        f"unexpected status {r.status_code} from {_DEVICE_AUTHORIZE_PATH}: {desc}"
    )


def _parse_token_response(body: dict[str, Any]) -> DeviceCodeSetupResult:
    """200 OK token response → DeviceCodeSetupResult (plain device code 用、 RAR なし)."""
    try:
        access_token = str(body["access_token"])
        expires_in = int(body["expires_in"])
    except (KeyError, ValueError) as exc:
        raise InstallError(f"malformed token response: missing field {exc}") from exc

    refresh_token_raw = body.get("refresh_token")
    refresh_token = str(refresh_token_raw) if refresh_token_raw else None

    scope_raw = str(body.get("scope", ""))
    scope_tuple = tuple(scope_raw.split()) if scope_raw else ()

    id_token_raw = body.get("id_token")
    id_token = str(id_token_raw) if id_token_raw else None

    return DeviceCodeSetupResult(
        access_token=access_token,
        refresh_token=refresh_token,
        scope=scope_tuple,
        expires_in=expires_in,
        id_token=id_token,
        granted_scopes=scope_tuple,  # legacy alias
        granted_at_unix=time.time(),
    )


__all__ = [
    "DeviceCodeSetupPending",
    "DeviceCodeSetupResult",
    "InstallAbortedError",
    "InstallAuthError",
    "InstallDeniedError",
    "InstallError",
    "InstallTimeoutError",
    "OnUserCodeCallback",
    "setup_via_device_code",
]
