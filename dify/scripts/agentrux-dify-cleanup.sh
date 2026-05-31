#!/usr/bin/env bash
# agentrux-dify-cleanup.sh
# Deletes ALL AgenTrux tool credentials and ALL AgenTrux trigger subscriptions
# from the Dify workspace, via Dify's console delete endpoints.
#
# Why: Dify's frontend (mid UI-platform migration, v1.14.x) does not reliably
# expose a delete action for tool-provider credentials, so they accumulate.
# The backend delete API works fine — this script drives it.
#
# NOTE on Dify's API: tool-credential management is a Console API (the same one
# the web UI calls), not part of Dify's documented public Service API. There is
# no official public reference for it. This script uses the same endpoints:
#   POST /console/api/workspaces/current/tool-provider/builtin/<provider>/delete
#   POST /console/api/workspaces/current/trigger-provider/<sub_id>/subscriptions/delete
#
# Auth: Dify console login is RSA-encrypted, so a short-lived console JWT is
# minted via Dify's own AccountService inside the api container. Token minting
# (which imports app_factory + gevent) and the HTTP deletes are run in SEPARATE
# python processes — gevent's monkey-patching breaks urllib in the same process.
#
# Usage (on the Dify VM):  bash agentrux-dify-cleanup.sh
set -euo pipefail

ACCOUNT_EMAIL="${ACCOUNT_EMAIL:-agentruxcorp@gmail.com}"
API_CONTAINER="${API_CONTAINER:-docker-api-1}"

# --- Step 1: mint a console JWT (+ CSRF token) inside the api container ---
TOKENS="$(docker exec -e ACCOUNT_EMAIL="$ACCOUNT_EMAIL" "$API_CONTAINER" python3 -c '
import os, app_factory
app = app_factory.create_app()[0].wsgi_app
with app.app_context():
    from extensions.ext_database import db
    from models.account import Account
    from services.account_service import AccountService
    from libs.token import generate_csrf_token
    acc = db.session.query(Account).filter(Account.email == os.environ["ACCOUNT_EMAIL"]).first()
    if not acc:
        raise SystemExit("account not found: " + os.environ["ACCOUNT_EMAIL"])
    print(AccountService.get_account_jwt_token(account=acc))
    print(generate_csrf_token(acc.id))
')"
ATOK="$(printf '%s\n' "$TOKENS" | sed -n 1p)"
CTOK="$(printf '%s\n' "$TOKENS" | sed -n 2p)"
if [ -z "$ATOK" ] || [ -z "$CTOK" ]; then
  echo "failed to mint console token" >&2
  exit 1
fi

# --- Step 2: list + delete via HTTP in a CLEAN process (no app_factory/gevent) ---
docker exec -e ATOK="$ATOK" -e CTOK="$CTOK" "$API_CONTAINER" python3 -c '
import os, json, urllib.request, urllib.parse, urllib.error
ATOK = os.environ["ATOK"]; CTOK = os.environ["CTOK"]
H = {"Authorization": "Bearer " + ATOK, "X-CSRF-Token": CTOK, "Cookie": "__Host-csrf_token=" + CTOK}
HP = dict(H); HP["Content-Type"] = "application/json"
B = "http://localhost:5001/console/api/workspaces/current"

def get(u):
    return json.load(urllib.request.urlopen(urllib.request.Request(u, headers=H), timeout=25))

def post(u, body):
    return urllib.request.urlopen(
        urllib.request.Request(u, data=json.dumps(body).encode(), headers=HP, method="POST"), timeout=25
    ).read().decode()

TOOL = urllib.parse.quote("agentrux/agentrux-tools/agentrux_tools", safe="")
TRIG = urllib.parse.quote("agentrux/agentrux-trigger/agentrux-trigger", safe="")

creds = get(B + "/tool-provider/builtin/" + TOOL + "/credentials")
print("tool credentials found:", len(creds))
for c in creds:
    try:
        post(B + "/tool-provider/builtin/" + TOOL + "/delete", {"credential_id": c["id"]})
        print("  deleted credential:", c.get("name"), c["id"])
    except urllib.error.HTTPError as e:
        print("  FAIL credential", c.get("name"), e.code, e.read().decode()[:120])

subs = get(B + "/trigger-provider/" + TRIG + "/subscriptions/list")
print("trigger subscriptions found:", len(subs))
for s in subs:
    try:
        post(B + "/trigger-provider/" + urllib.parse.quote(s["id"], safe="") + "/subscriptions/delete", {})
        print("  deleted subscription:", s.get("name"), s["id"])
    except urllib.error.HTTPError as e:
        print("  FAIL subscription", s["id"], e.code, e.read().decode()[:120])

print("remaining credentials:", len(get(B + "/tool-provider/builtin/" + TOOL + "/credentials")))
print("remaining subscriptions:", len(get(B + "/trigger-provider/" + TRIG + "/subscriptions/list")))
'
