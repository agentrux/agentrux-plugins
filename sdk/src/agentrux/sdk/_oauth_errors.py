"""OAuth (RFC 6749 / RFC 8628) error parsing + Install* error hierarchy.

SSOT: docs/04_design/auth/device_code_setup_v1.md §5-1 (Step 1a 抽出のみ)

本 module は `topology_install.py` から 「error parsing」 + 「error 階層」 を pure-move
したもの。 挙動は変えない (Codex round 1 MF-5 / round 2 MF-2 反映)。

新 helper `setup_via_device_code()` (device_code_setup.py) も本 module の Install* 系を
再利用する (1 つの try / except で plain device code と RAR 両方を扱えるようにする方針)。
"""

from __future__ import annotations

import httpx

from agentrux.sdk.errors import AgenTruxError


def parse_oauth_error(r: httpx.Response) -> tuple[str, str]:
    """response body から (error_code, error_description) を取り出す.

    FastAPI HTTPException shape `{"detail": {"error", "error_description"}}` と
    OAuth RFC 6749 shape `{"error", "error_description"}` の両方を受け入れる。
    parse 失敗時は (空文字, response text 先頭 200 chars) を返す (caller が status_code
    + http error code でフォールバック判定する想定)。
    """
    try:
        parsed = r.json()
    except Exception:
        return "", r.text[:200]
    if not isinstance(parsed, dict):
        return "", str(parsed)[:200]
    # FastAPI nested shape
    detail = parsed.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("error", "")), str(detail.get("error_description", ""))
    # OAuth flat shape
    return str(parsed.get("error", "")), str(parsed.get("error_description", ""))


# ----------------------------------------------------------------------------
# Error hierarchy (topology_install.py から pure-move、 公開 API)
# ----------------------------------------------------------------------------


class InstallError(AgenTruxError):
    """Plugin install / OAuth setup で発生する致命的エラー (timeout / denied / invalid_request).

    Topology Flow v1 (`install_topology()`) と plain device code (`setup_via_device_code()`)
    両方で raise される。 caller は base class で catch + isinstance で派生種別を分岐可。
    """


class InstallDeniedError(InstallError):
    """user が picker で deny した、 または backend が access_denied を返した."""


class InstallTimeoutError(InstallError):
    """user が timeout 内に承認しなかった (RFC 8628 expired_token)."""


class InstallAuthError(InstallError):
    """OAuth client_id が無効 (invalid_client). TS InstallAuthError と symmetric."""


class InstallAbortedError(InstallError):
    """caller が asyncio.cancel / KeyboardInterrupt で polling を中断 (TS InstallAbortedError symmetric)."""


__all__ = [
    "InstallAbortedError",
    "InstallAuthError",
    "InstallDeniedError",
    "InstallError",
    "InstallTimeoutError",
    "parse_oauth_error",
]
