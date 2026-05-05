# AgenTrux for Dify

Native plugin that lets Dify (Cloud / self-hosted ≥ 1.10) talk to AgenTrux PubSub topics.

> Japanese version: [`README.md`](./README.md)

## What changed in v1.0.0

The legacy `v0.x` plugin used a one-shot **Activation Code (`ac_...`)** that was redeemed against the old `/auth/activate` endpoint. That endpoint has been **removed** with the new OAuth 2.1 auth layer.

| Plugin version | Auth method | Server |
|----------------|-------------|--------|
| `v0.x`         | Activation Code → `/auth/activate` (legacy)               | retired |
| **`v1.0.0`**   | OAuth 2.1 (Authorization Code + PKCE) *or* `client_credentials` | **production** |

The older `v0.2.x` / `v0.3.x` `.difypkg` files are kept for backward reference only — they will not authenticate against the production server. **Use `v1.0.0` for new installs.**

## Install

1. Dify → **Studio → Tools → Install Plugin → Local Package**
2. Upload `dify-agentrux-tools-1.0.0.difypkg`
3. Click **Authorize / Connect** on the plugin details page

## Two auth modes

The plugin ships with **both** `oauth_schema` and `credentials_for_provider` so you can pick whichever fits your Dify deployment.

### A. OAuth Authorization Code + PKCE (recommended)

Best for Dify Cloud and any self-hosted Dify with a public callback URL.

1. **Register an OAuth client** on AgenTrux Console (Settings → OAuth Clients → Register), or via API:
   ```bash
   curl -X POST https://api.agentrux.com/oauth/register \
     -H 'Content-Type: application/json' \
     -d '{
       "client_name": "Dify Plugin",
       "redirect_uris": ["<callback URL Dify shows you>"],
       "grant_types": ["authorization_code", "refresh_token"]
     }'
   ```
   You receive `client_id` (`oauth-client_<uuid>`) and optionally a `client_secret`.
2. In Dify, **Authorize** → **OAuth** tab.
3. Fill `base_url` (`https://api.agentrux.com`), `client_id`, and `client_secret` (leave blank for public PKCE clients).
4. **Connect** → AgenTrux consent page → **Allow**.
5. Dify stores the JWT and auto-rotates via `refresh_token` before expiry.

### B. `client_credentials` fallback

Use when your Dify cannot receive a public callback (NAT-bound self-host, etc.).

1. AgenTrux Console → Scripts → **Create Credential**
2. Copy the `client_id` (`script_<uuid>`) and `client_secret`
3. In Dify, **Authorize** → **API Key** tab
4. Paste `base_url`, `client_id`, `client_secret` → **Save**

> The `client_id` **must** carry the `script_` prefix. Bare UUIDs are rejected server-side.

## Tools

| Tool | Action | Required scope |
|------|--------|----------------|
| `agentrux_publish` | Publish an event to a topic     | `topic.write` |
| `agentrux_read`    | Read recent events from a topic | `topic.read` |
| `agentrux_upload`  | Upload binary payload (≤ 15 MB) to a topic | `topic.write` |

`topic_id` is a `dynamic-select` populated from the JWT's `scope` claim — write tools only list write-granted topics, read tool only lists read-granted topics.

## About the legacy `v0.x` artifacts

- The shipped `dify-agentrux-tools-0.2.0` / `0.2.1` / `0.3.0` `.difypkg` files are **deprecated**.
- They cannot authenticate against the new server (`/auth/activate` is gone).
- Migrating: **uninstall the old plugin → install `v1.0.0` → Authorize**.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `client_id must start with 'script_'` | Pasted a raw Console UUID | Use the prefixed ID issued by Console |
| `AgenTrux rejected client_credentials (HTTP 401)` | Wrong/revoked secret | Re-issue via Console |
| `OAuth state mismatch` | Plugin process restarted between authorize and callback | Retry Authorize |
| Topic not in dynamic-select | JWT scope lacks the grant | Create a Grant in Console |

## Developer notes

- Sources: `provider/agentrux_tools.py` (OAuth methods), `provider/agentrux_api.py` (runtime)
- Unit tests: `tests/` (`pytest -x`)
- Minimum Dify: **1.10.0**
- Minimum `dify_plugin` SDK: **0.4.2**
