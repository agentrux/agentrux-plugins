"""Topology Flow setup CLI — script-initiated auth + topology declaration in 1 step.

SSOT: docs/04_design/auth/topology_request_v1.md + device_code_setup_v1.md §0-3 (vs plain)

Why this CLI exists (over `mcp_setup_cli.py`):
  - plain device code は credential のみ取得、 topics/grants は別途 Console で作成 = 2 step
  - **Topology Flow は 1 step**: plugin が topology を declare → Console picker で user が
    alias / topic resolution / grant approve を確定 → 1 TX で Script + Topics + Grants 作成 +
    token 発行。 user は copy-paste 不要 (CLI が browser を auto-open)。

Usage:
    $ python -m agentrux.sdk.topology_setup_cli \\
        --base-url https://api.agentrux.com \\
        --client-name "Cursor on MacBook" \\
        --script-name "weather-bot" \\
        --description "WeatherAPI を Composer に流す" \\
        --topic "weather-data:write" \\
        --topic "weather-data:read"

`--topic NAME:SCOPE[:BINDING]` で 1 grant を宣言:
  - `weather-data:write` → topic ref="weather-data", scope="write", binding="weather-data-write"
  - `weather-data:write:weather-out` → binding 名 を指定
  - 同 topic に read+write 両方欲しいなら 2 回 --topic 指定 (`shared:read` `shared:write`)

Output:
  - Token bundle を ~/.agentrux/topology_<client-name-slug>.json に保存
  - Cursor / Claude Desktop MCP config snippet を stdout に出力
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

from agentrux.sdk.topology_install import (
    InstallAbortedError,
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


def _slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "topology"


def _credentials_path(client_name: str) -> pathlib.Path:
    home = pathlib.Path(os.environ.get("HOME", os.environ.get("USERPROFILE", ".")))
    return home / ".agentrux" / f"topology_{_slug(client_name)}.json"


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
            "User-Agent": "agentrux-sdk-topology-setup/1.0",
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


def _parse_topic_arg(spec: str) -> tuple[str, str, str | None]:
    """`name:scope[:binding]` → (name, scope, binding_or_None)."""
    parts = spec.split(":")
    if len(parts) < 2 or len(parts) > 3:
        raise ValueError(
            f"--topic format must be NAME:SCOPE[:BINDING], got {spec!r}"
        )
    name, scope = parts[0], parts[1]
    binding = parts[2] if len(parts) == 3 else None
    if scope not in ("read", "write"):
        raise ValueError(f"scope must be 'read' or 'write', got {scope!r}")
    return name, scope, binding


def _build_declaration(args: argparse.Namespace) -> TopologyDeclaration:
    """CLI args から TopologyDeclaration を構築.

    複数 --topic 指定 で 1 topic に複数 grants (read+write 等) を同 ref で集約。
    """
    topic_specs: dict[str, dict] = {}  # ref → {name, retention_s, intent}
    grant_specs: list[TopologyGrantSpec] = []
    for raw in args.topic:
        name, scope, binding = _parse_topic_arg(raw)
        ref = name  # CLI では simplification: ref == name
        if ref not in topic_specs:
            topic_specs[ref] = {
                "ref": ref,
                "name": name,
                "retention_s": args.retention,
                "intent": None,
            }
        # binding_name default は "<ref>-<scope>" (DDL CHECK 整合: ascii non-control 1-64)
        binding_name = binding or f"{ref}-{scope}"
        grant_specs.append(
            TopologyGrantSpec(
                topic_ref=ref,
                scope=scope,  # type: ignore[arg-type]
                binding_name=binding_name,
            )
        )

    topics = tuple(
        TopologyTopicSpec(
            ref=v["ref"],
            name=v["name"],
            retention_s=v["retention_s"],
            intent=v["intent"],
        )
        for v in topic_specs.values()
    )
    return TopologyDeclaration(
        script_name=args.script_name,
        description=args.description,
        topics=topics,
        grants=tuple(grant_specs),
    )


def _on_user_code(info: InstallPendingInfo, auto_open: bool = True) -> None:
    print("", file=sys.stderr)
    print("  ╔═══════════════════════════════════════════════════════════════╗", file=sys.stderr)
    print("  ║  Console picker で alias / topic / grant を選択してください:    ║", file=sys.stderr)
    print(f"  ║    {info.verification_uri_complete:<60} ║", file=sys.stderr)
    print(f"  ║  または user_code を手動入力: {info.user_code:<30} ║", file=sys.stderr)
    print("  ╚═══════════════════════════════════════════════════════════════╝", file=sys.stderr)
    if auto_open:
        try:
            import webbrowser

            if webbrowser.open(info.verification_uri_complete, new=2):
                print("  ↳ Opening browser automatically...", file=sys.stderr)
        except Exception as e:
            print(
                f"  ↳ webbrowser.open() failed ({e!r}) — open manually.",
                file=sys.stderr,
            )
    print("", file=sys.stderr)


async def _async_main(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")

    # 1) Build declaration (validate before DCR)
    try:
        declaration = _build_declaration(args)
    except (ValueError, Exception) as e:  # ConfigError も catch
        print(f"Declaration build failed: {e}", file=sys.stderr)
        return 1
    print(
        f"[1/3] Topology declaration: script={declaration.script_name!r} "
        f"topics={len(declaration.topics)} grants={len(declaration.grants)}",
        file=sys.stderr,
    )

    # 2) DCR
    print(f"[2/3] DCR (POST /oauth/register) client_name={args.client_name!r}", file=sys.stderr)
    try:
        dcr_client_id = _dcr_register(base_url, args.client_name)
    except RuntimeError as e:
        print(f"DCR failed: {e}", file=sys.stderr)
        return 2
    print(f"      dcr_client_id={dcr_client_id}", file=sys.stderr)

    # 3) install_topology (POST /oauth/topology-request + polling)
    print("[3/3] Topology install (POST /oauth/topology-request → poll /oauth/token)", file=sys.stderr)
    try:
        result: InstallResult = await install_topology(
            base_url=base_url,
            client_id=dcr_client_id,
            declaration=declaration,
            on_user_code=lambda info: _on_user_code(info, auto_open=not args.no_auto_open),
            timeout=args.timeout,
        )
    except InstallDeniedError as e:
        print(f"Setup denied: {e}", file=sys.stderr)
        return 3
    except InstallTimeoutError as e:
        print(f"Setup timed out: {e}", file=sys.stderr)
        return 4
    except (InstallAuthError, InstallAbortedError) as e:
        print(f"Setup error ({type(e).__name__}): {e}", file=sys.stderr)
        return 5
    except InstallError as e:
        print(f"Setup failed: {e}", file=sys.stderr)
        return 6

    # 4) Save token bundle + topology_result
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
        "script_id": result.script_id,
        "alias_id": result.alias_id,
        "topic_id_map": result.topic_id_map,
        "grants": [
            {
                "topic_scope_key": g.topic_scope_key,
                "grant_id": g.grant_id,
                "binding_name": g.binding_name,
            }
            for g in result.grants
        ],
    }
    creds_path.write_text(json.dumps(bundle, indent=2))
    os.chmod(str(creds_path), 0o600)
    print("", file=sys.stderr)
    print(f"✓ Setup complete. Token bundle saved to {creds_path}", file=sys.stderr)
    print(f"  script_id: {result.script_id}", file=sys.stderr)
    print(f"  alias_id:  {result.alias_id}", file=sys.stderr)
    for ref, topic_id in result.topic_id_map.items():
        print(f"  topic.{ref}: {topic_id}", file=sys.stderr)
    print("", file=sys.stderr)

    # 5) Output MCP config snippet (stdout) for caller convenience
    config_snippet = {
        "mcpServers": {
            "agentrux": {
                "url": f"{base_url}/mcp",
                "auth": {
                    "type": "bearer",
                    "tokenStorage": f"agentrux-keychain://{_slug(args.client_name)}",
                },
            }
        }
    }
    print(json.dumps(config_snippet, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentrux topology-setup",
        description=(
            "Set up AgenTrux via Topology Request Flow v1 (RFC 8628 + RFC 9396 RAR). "
            "Declares Script + Topics + Grants upfront, opens Console picker in browser, "
            "user approves → all resources created + token issued in 1 step."
        ),
    )
    parser.add_argument("--base-url", required=True, help="AgenTrux API base URL")
    parser.add_argument(
        "--client-name", required=True, help="DCR client_name (e.g. 'Cursor on MacBook')"
    )
    parser.add_argument("--script-name", required=True, help="Script name to create (e.g. 'weather-bot')")
    parser.add_argument(
        "--description",
        default="(no description)",
        help="Script description shown to operator in picker",
    )
    parser.add_argument(
        "--topic",
        action="append",
        required=True,
        help=(
            "Topic declaration: 'NAME:SCOPE[:BINDING]' (e.g. 'weather-data:write' "
            "or 'shared:read:in-stream'). Repeat for multiple grants/topics."
        ),
    )
    parser.add_argument(
        "--retention",
        type=int,
        default=86400,
        help="Topic retention seconds (default 86400 = 1 day)",
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
