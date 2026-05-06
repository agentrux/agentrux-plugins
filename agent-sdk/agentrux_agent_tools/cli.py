"""AgenTrux SDK command-line entry point.

Implements `agentrux login` (RFC 8628 OAuth Device Authorization Grant)
so the user never has to copy-paste credential pairs out of the Console.

Flow:
  1. POST /oauth/device_authorization → device_code, user_code, verification_uri
  2. Print user_code + URL, optionally open the browser.
  3. Poll /oauth/token with grant_type=device_code until approved or expired.
  4. Save the resulting access_token / refresh_token to ~/.agentrux/credentials.

The token endpoint returns a Bearer access_token (short-lived) plus a
refresh_token (long-lived). We store both — runtime SDK code reads the
access_token, falls back to the refresh_token when it expires. The
script_id is parsed out of the JWT's payload so callers can introspect
which Script the device is acting as without keeping that as a separate
piece of state to drift from the token.

The saved file format is a key=value INI (no third-party deps — Python
stdlib's configparser handles it). The default profile is "default";
`--profile <name>` lets a developer keep separate credentials for
prod / staging without juggling env vars.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path


def _jwt_payload(token: str) -> dict:
    """Decode a JWT's payload without verifying the signature.

    We use this purely to surface display-only fields (e.g. which
    Script the device is acting as). The backend signature is verified
    server-side on every API call, so a tampered local token would
    just fail at request time.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    pad = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad).decode("utf-8"))
    except Exception:
        return {}

DEFAULT_BASE_URL = "https://api.agentrux.com"
CREDENTIALS_PATH = Path.home() / ".agentrux" / "credentials"
# DCR-registered client_id for THIS CLI install, cached so subsequent
# `agentrux login` calls reuse it (one DB row per install, not per
# login). Public client (no secret) — PKCE / device_code flow does
# not need a confidential client.
CLIENT_REGISTRATION_PATH = Path.home() / ".agentrux" / "cli-client.json"


def _ensure_registered_client(base_url: str) -> str:
    """Return the DCR-registered client_id for this CLI install.

    Lazily registers via /oauth/register on first call and caches the
    result to ~/.agentrux/cli-client.json. The cache is keyed by
    base_url so a developer who switches between staging and prod
    won't reuse a staging client_id against prod (Stripe / AWS profile
    pattern).
    """
    cache: dict[str, str] = {}
    if CLIENT_REGISTRATION_PATH.exists():
        try:
            cache = json.loads(CLIENT_REGISTRATION_PATH.read_text())
        except Exception:
            cache = {}
    if base_url in cache:
        return cache[base_url]

    # Register. redirect_uris is required by RFC 7591 even for clients
    # that won't use the authorization_code flow — pass a loopback
    # placeholder.
    body = json.dumps({
        "client_name": "AgenTrux CLI",
        "redirect_uris": ["http://127.0.0.1:0/cli-unused"],
        "grant_types": [
            "urn:ietf:params:oauth:grant-type:device_code",
            "refresh_token",
        ],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/oauth/register",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "agentrux-cli/0.1"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Don't dump the body — only surface the HTTP code, the rest
        # could include server-internal hints we shouldn't leak through
        # a public CLI binary.
        raise RuntimeError(
            f"Could not register CLI as OAuth client (HTTP {e.code})"
        ) from e
    client_id = payload.get("client_id")
    if not client_id:
        raise RuntimeError(f"DCR did not return client_id: {payload}")

    cache[base_url] = client_id
    CLIENT_REGISTRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_REGISTRATION_PATH.write_text(json.dumps(cache, indent=2))
    os.chmod(CLIENT_REGISTRATION_PATH, 0o600)
    return client_id


def _post(url: str, data: dict, timeout: float = 10.0) -> tuple[int, dict]:
    """Issue a form-encoded POST and parse the JSON response.

    We avoid pulling in `requests` so the CLI stays lightweight and
    installs cleanly into restricted-egress environments. The error
    handling treats 4xx as "expected" (Stripe-style: read the body for
    a code) and 5xx as transient.
    """
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            payload = {"error": "http_error", "status": e.code}
        return e.code, payload


def _save_credentials(
    profile: str,
    base_url: str,
    script_id: str,
    access_token: str,
    refresh_token: str,
    expires_at: int,
) -> Path:
    """Write the OAuth tokens to ~/.agentrux/credentials, mode 0600.

    Mirrors the AWS/gh CLI convention (~/.aws/credentials,
    ~/.config/gh/hosts.yml) so users immediately know what to grep for
    when they audit local secrets. expires_at is unix epoch — runtime
    SDK code compares to wall clock to decide refresh.
    """
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    cfg = configparser.ConfigParser()
    if CREDENTIALS_PATH.exists():
        cfg.read(CREDENTIALS_PATH)
    cfg[profile] = {
        "base_url": base_url,
        "script_id": script_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": str(expires_at),
    }
    with open(CREDENTIALS_PATH, "w") as f:
        cfg.write(f)
    os.chmod(CREDENTIALS_PATH, 0o600)
    return CREDENTIALS_PATH


def cmd_login(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    # Lazy DCR: the first login on a host registers a public OAuth
    # client; subsequent logins reuse that client_id. Skipping if the
    # user passed --client-id (e.g. an operator-pre-registered one).
    if args.client_id:
        client_id = args.client_id
    else:
        try:
            client_id = _ensure_registered_client(base_url)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    # 1. Start the device authorization. The backend assigns a
    # device_code (kept private to this process) and a user_code (the
    # human-friendly XXXX-XXXX string we display).
    status, payload = _post(
        f"{base_url}/oauth/device_authorization",
        {"client_id": client_id, "scope": args.scope or ""},
    )
    if status >= 400:
        # Surface OAuth's standard error fields only; never the full
        # JSON (avoids leaking server-internal details if the backend
        # ever expands the error payload).
        err = (payload or {}).get("error", f"http_{status}")
        desc = (payload or {}).get("error_description", "")
        print(f"error: device_authorization failed: {err} {desc}".strip(), file=sys.stderr)
        return 1
    device_code = payload["device_code"]
    user_code = payload["user_code"]
    verification_uri = payload["verification_uri"]
    verification_uri_complete = payload.get("verification_uri_complete") or verification_uri
    expires_in = int(payload.get("expires_in", 600))
    interval = max(1, int(payload.get("interval", 5)))

    print()
    print(f"  Visit:  {verification_uri}")
    print(f"  Code:   {user_code}")
    print()
    print(f"  (Or open the prefilled link: {verification_uri_complete})")
    print()
    if not args.no_browser:
        try:
            webbrowser.open(verification_uri_complete, new=2)
        except Exception:
            pass

    # 2. Poll for completion. RFC 8628 §3.5: respect `interval`, but if
    # the server returns slow_down, double the interval. Stop on auth
    # success, denial, or expiry.
    deadline = time.time() + expires_in
    sys.stdout.write("Waiting for browser approval")
    sys.stdout.flush()
    while time.time() < deadline:
        time.sleep(interval)
        sys.stdout.write(".")
        sys.stdout.flush()
        status, token_payload = _post(
            f"{base_url}/oauth/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
        )
        if status == 200:
            print()
            # The /oauth/token device_code grant returns the standard
            # OAuth response: access_token + refresh_token + scope.
            # Pull script_id from the JWT's claims (it is added under
            # "client_id" or "sub" depending on issuance path) so the
            # CLI doesn't have to make a follow-up /me call.
            access_token = token_payload.get("access_token", "")
            refresh_token = token_payload.get("refresh_token", "") or ""
            expires_in = int(token_payload.get("expires_in", 3600) or 3600)
            if not access_token:
                print(
                    f"error: token response missing access_token: {token_payload}",
                    file=sys.stderr,
                )
                return 1
            claims = _jwt_payload(access_token)
            script_id = (
                claims.get("client_id")
                or claims.get("script_id")
                or claims.get("sub")
                or ""
            )
            expires_at = int(time.time()) + expires_in
            path = _save_credentials(
                args.profile, base_url, script_id, access_token, refresh_token, expires_at,
            )
            print(f"✓ Authorized as Script {script_id or '(unknown)'}.")
            print(f"  Tokens saved to {path} [{args.profile}]")
            return 0
        err = (token_payload or {}).get("error", "")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval *= 2
            continue
        if err in {"access_denied", "expired_token", "invalid_grant"}:
            print()
            print(f"error: {err}", file=sys.stderr)
            return 1
        # Unknown 4xx — surface it so we don't poll forever.
        if status >= 400:
            print()
            err_name = (token_payload or {}).get("error", f"http_{status}")
            err_desc = (token_payload or {}).get("error_description", "")
            print(f"error: token poll failed: {err_name} {err_desc}".strip(), file=sys.stderr)
            return 1

    print()
    print("error: login timed out — re-run `agentrux login`", file=sys.stderr)
    return 1


def cmd_whoami(args: argparse.Namespace) -> int:
    """Print the active credential profile (without leaking the secret)."""
    if not CREDENTIALS_PATH.exists():
        print(f"no credentials at {CREDENTIALS_PATH}", file=sys.stderr)
        return 1
    cfg = configparser.ConfigParser()
    cfg.read(CREDENTIALS_PATH)
    profile = args.profile
    if profile not in cfg:
        print(f"profile {profile!r} not found in {CREDENTIALS_PATH}", file=sys.stderr)
        return 1
    sec = cfg[profile]
    print(f"profile:       {profile}")
    print(f"base_url:      {sec.get('base_url', '')}")
    print(f"script_id:     {sec.get('script_id', '')}")
    expires_at_s = sec.get("expires_at", "")
    if expires_at_s:
        try:
            ts = int(expires_at_s)
            now = int(time.time())
            remaining = ts - now
            state = "valid" if remaining > 0 else "expired"
            print(f"access_token:  {state} ({remaining}s remaining)")
        except ValueError:
            print(f"access_token:  unknown (expires_at={expires_at_s!r})")
    refresh = sec.get("refresh_token", "")
    if refresh:
        # Length only — the refresh_token is long-lived, never display
        # in a shared terminal.
        print(f"refresh_token: present ({len(refresh)} chars)")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Drop a profile from ~/.agentrux/credentials."""
    if not CREDENTIALS_PATH.exists():
        return 0
    cfg = configparser.ConfigParser()
    cfg.read(CREDENTIALS_PATH)
    if args.profile not in cfg:
        return 0
    cfg.remove_section(args.profile)
    with open(CREDENTIALS_PATH, "w") as f:
        cfg.write(f)
    print(f"Removed profile {args.profile!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentrux", description="AgenTrux SDK CLI")
    p.add_argument("--base-url", default=os.environ.get("AGENTRUX_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--profile", default=os.environ.get("AGENTRUX_PROFILE", "default"))
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="Authenticate this machine via OAuth device flow")
    login.add_argument("--client-id", default=None, help="Override DCR (defaults to auto-registered CLI client)")
    login.add_argument("--scope", default="", help="Space-separated scope list (default: server picks)")
    login.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    login.set_defaults(func=cmd_login)

    who = sub.add_parser("whoami", help="Show the active credential profile")
    who.set_defaults(func=cmd_whoami)

    out = sub.add_parser("logout", help="Drop a credential profile")
    out.set_defaults(func=cmd_logout)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
