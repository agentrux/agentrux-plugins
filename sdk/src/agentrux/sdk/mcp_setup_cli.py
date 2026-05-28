"""MCP client setup CLI — runs RFC 8628 device flow + outputs MCP server config snippet.

SSOT: docs/04_design/auth/device_code_setup_v1.md §4-3

Usage:
    $ python -m agentrux.sdk.mcp_setup_cli \\
        --base-url https://api.agentrux.com \\
        --client-name "Cursor on MacBook Pro" \\
        --scope topic.read,topic.write

Flow:
    1. DCR (POST /oauth/register) で public client を取得
    2. setup_via_device_code() を呼んで device flow + polling
    3. Token bundle を保存 (default: ~/.agentrux/mcp_<client_name_slug>.json、 0o600)
    4. Cursor / Claude Desktop MCP server config の snippet を stdout に表示

Token storage (Codex round 1 MF-7):
    - default は file (host machine ID 派生鍵で AES-256-GCM 暗号化、 plugins_design.md §13-2)
    - OS keychain integration は v2 (keyring module を optional dep として future phase)
    - 本 v1 では plain JSON 0o600 (v2 で暗号化に切替予定、 spec §7 Step 7 で docs realign 時に
      transition timeline を確定)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from agentrux.sdk.device_code_setup import (
    DeviceCodeSetupResult,
    InstallAuthError,
    InstallDeniedError,
    InstallError,
    InstallTimeoutError,
    setup_via_device_code,
)


def _slug(name: str) -> str:
    """client_name を file-safe slug に変換 (e.g. 'Cursor on MacBook' → 'cursor-on-macbook')."""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "mcp"


def _credentials_path(client_name: str) -> pathlib.Path:
    home = pathlib.Path(os.environ.get("HOME", os.environ.get("USERPROFILE", ".")))
    return home / ".agentrux" / f"mcp_{_slug(client_name)}.json"


def _dcr_register(base_url: str, client_name: str) -> str:
    """POST /oauth/register で public client (token_endpoint_auth_method=none) を作る."""
    body = json.dumps(
        {"client_name": client_name, "token_endpoint_auth_method": "none"}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/oauth/register",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agentrux-sdk-mcp-setup/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 201:
                raise RuntimeError(
                    f"DCR failed (status={resp.status}): {resp.read().decode()}"
                )
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"DCR failed (status={e.code}): {e.read().decode(errors='replace')}"
        ) from None
    if "client_id" not in data:
        raise RuntimeError(f"DCR response missing client_id: {data}")
    return str(data["client_id"])


def _print_user_code(user_code: str, url: str, auto_open: bool = True) -> None:
    """on_user_code callback: operator に分かりやすく表示 + browser auto-open.

    `auto_open=True` (default) なら webbrowser.open() で URL を auto open (= user は
    copy-paste 不要)。 fallback として URL は stderr にも表示 (browser が無い CI / SSH 環境用)。
    """
    print("", file=sys.stderr)
    print("  ╔═══════════════════════════════════════════════════════════════╗", file=sys.stderr)
    print("  ║  Open this URL in your browser to approve the MCP setup:      ║", file=sys.stderr)
    print(f"  ║    {url:<60} ║", file=sys.stderr)
    print(f"  ║  Or enter user_code manually: {user_code:<32} ║", file=sys.stderr)
    print("  ╚═══════════════════════════════════════════════════════════════╝", file=sys.stderr)
    if auto_open:
        try:
            import webbrowser

            opened = webbrowser.open(url, new=2)
            if opened:
                print("  ↳ Opening browser automatically...", file=sys.stderr)
            else:
                print(
                    "  ↳ webbrowser.open() returned False — please open the URL manually.",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"  ↳ webbrowser.open() failed ({e!r}) — please open the URL manually.",
                file=sys.stderr,
            )
    print("", file=sys.stderr)


async def _async_main(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    scope = [s.strip() for s in args.scope.split(",") if s.strip()]

    # 1) DCR
    print(f"[1/3] DCR (POST /oauth/register) client_name={args.client_name!r}", file=sys.stderr)
    try:
        dcr_client_id = _dcr_register(base_url, args.client_name)
    except RuntimeError as e:
        print(f"DCR failed: {e}", file=sys.stderr)
        return 1
    print(f"      dcr_client_id={dcr_client_id}", file=sys.stderr)

    # 2) Device flow setup
    print("[2/3] Device flow (POST /oauth/device/authorize → poll /oauth/token)", file=sys.stderr)
    try:
        result: DeviceCodeSetupResult = await setup_via_device_code(
            base_url=base_url,
            client_id=dcr_client_id,
            scope=scope,
            on_user_code=lambda info: _print_user_code(
                info.user_code,
                info.verification_uri_complete,
                auto_open=not args.no_auto_open,
            ),
            timeout=args.timeout,
        )
    except InstallDeniedError as e:
        print(f"Setup denied: {e}", file=sys.stderr)
        return 3
    except InstallTimeoutError as e:
        print(f"Setup timed out: {e}", file=sys.stderr)
        return 4
    except InstallAuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        return 5
    except InstallError as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        return 6

    # 3) Save token bundle
    print("[3/3] Saving credentials", file=sys.stderr)
    creds_path = _credentials_path(args.client_name)
    creds_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    bundle = {
        "base_url": base_url,
        "client_name": args.client_name,
        "dcr_client_id": dcr_client_id,
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "expires_in": result.expires_in,
        "issued_at_unix": int(result.granted_at_unix or time.time()),
        "scope": list(result.scope),
    }
    if result.id_token:
        bundle["id_token"] = result.id_token
    creds_path.write_text(json.dumps(bundle, indent=2))
    os.chmod(str(creds_path), 0o600)
    print(f"      saved to {creds_path}", file=sys.stderr)
    print("", file=sys.stderr)

    # 4) Output MCP server config snippet (stdout)
    print("Done. Add the following to your MCP client config (Cursor / Claude Desktop):", file=sys.stderr)
    print("", file=sys.stderr)
    config_snippet = {
        "mcpServers": {
            "agentrux": {
                "url": f"{base_url}/mcp",
                "auth": {
                    "type": "bearer",
                    "tokenStorage": f"agentrux-keychain://{_slug(args.client_name)}",
                    # v1 fallback: plain JSON file at the path above (decrypted in v1、
                    # AES-256-GCM in v2 per spec §4-3 token storage)。 MCP client は
                    # tokenStorage URI を自身で解決する想定。
                },
            }
        }
    }
    print(json.dumps(config_snippet, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentrux mcp setup",
        description=(
            "Set up an MCP client connection to AgenTrux using RFC 8628 device flow "
            "(no RAR). Outputs the MCP server config snippet for Cursor / Claude Desktop."
        ),
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="AgenTrux API base URL (e.g. https://api.agentrux.com)",
    )
    parser.add_argument(
        "--client-name",
        required=True,
        help="DCR client_name (e.g. 'Cursor on MacBook Pro')",
    )
    parser.add_argument(
        "--scope",
        default="topic.read,topic.write",
        help="Comma-separated scope vocabulary (default: topic.read,topic.write)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Total polling timeout seconds [60, 600] (default 600)",
    )
    parser.add_argument(
        "--no-auto-open",
        action="store_true",
        help="Disable browser auto-open (CI / SSH-only environments)",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
