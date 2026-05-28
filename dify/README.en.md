# AgenTrux for Dify

Native plugin that lets Dify (Cloud / self-hosted ≥ 1.10) talk to AgenTrux PubSub topics.

> Japanese version: [`README.md`](./README.md)

## Packages

This repository publishes two Dify plugins:

| Package | Kind | Purpose | Latest .difypkg |
|---|---|---|---|
| **Tools** (`agentrux-tools`) | Tool plugin | A workflow / agent publishes to or reads from Topics | [`dify-agentrux-tools-1.1.0.difypkg`](./dify-agentrux-tools-1.1.0.difypkg) |
| **Trigger** (`agentrux-trigger`) | Trigger plugin | A workflow starts whenever a new event lands on a Topic | [`dify-agentrux-trigger-0.4.0.difypkg`](./dify-agentrux-trigger-0.4.0.difypkg) |

For a round-trip (Composer → Dify workflow edits message → publishes back to Composer) install both. Tools alone is enough for publish-only workflows.

## Version history (Tools)

| Plugin version | Auth method | Status |
|---|---|---|
| `v0.x` (~0.3.0) | Activation Code → `/auth/activate` (legacy) | Server endpoint retired; **does not work** |
| `v1.0.0` | OAuth 2.1 (Auth Code + PKCE) or `client_credentials`, hard-coded endpoint URL | Works but endpoint URL fixed |
| **`v1.1.0`** | OAuth 2.1 + [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414) `/.well-known/oauth-authorization-server` discovery | Recommended (see **known issue** below) |

`v1.1.0` introduced metadata discovery so the plugin keeps working even if AgenTrux's backend URL moves, without a plugin re-release.

### Known issue (v1.1.0)

In the `client_credentials` direct-paste mode (§B below) the plugin enforces a `script_` prefix on `client_id`. The AgenTrux server only ever issues `crd_<uuid>` form client IDs, so **§B does not currently work**.

→ Workaround: **use §A (OAuth Authorization Code + PKCE) instead** — that path is verified working.

## Install

### Tools plugin

1. Dify → **Studio → Tools → Install Plugin → Local Package**
2. Upload [`dify-agentrux-tools-1.1.0.difypkg`](./dify-agentrux-tools-1.1.0.difypkg)
3. On the plugin detail page click **Authorize / Connect** → follow §A below

### Trigger plugin

1. Dify → **Studio → Triggers → Install Plugin → Local Package**
2. Upload [`dify-agentrux-trigger-0.4.0.difypkg`](./dify-agentrux-trigger-0.4.0.difypkg)
3. In the workflow editor add **Trigger node → AgenTrux: New Event** → create a Subscription
4. In the Subscription form set `base_url` (= `https://api.agentrux.com`) and an Activation Code (`act_<base64>`, issued from Console)
5. Pick `delivery_mode`: **webhook** (when Dify is reachable from outside) or **sse** (NAT-bound self-host — the plugin opens an outbound SSE connection)

## Auth modes (Tools): two options

The plugin ships both `oauth_schema` and `credentials_for_provider`. **OAuth Authorization Code (PKCE)** is recommended.

### §A. OAuth Authorization Code + PKCE (recommended)

Use this on Dify Cloud and any self-hosted Dify with a public callback URL.

1. **Register an OAuth client via `POST /oauth/register` (RFC 7591 DCR)**

   ```bash
   curl -X POST https://api.agentrux.com/oauth/register \
     -H 'Content-Type: application/json' \
     -d '{
       "client_name": "My Dify Plugin",
       "redirect_uris": ["<callback URL Dify shows you>"],
       "grant_types": ["authorization_code", "refresh_token"],
       "token_endpoint_auth_method": "none"
     }'
   ```

   Response:
   ```json
   {
     "client_id": "dcr_<uuid>",
     "client_id_issued_at": ...,
     "registration_access_token": "rat_<base64>",
     ...
   }
   ```

   Note: AgenTrux Console does not currently expose an OAuth Clients list UI. Register either via `POST /oauth/register` directly, or reuse an OAuth Client created by the device flow (= agent-sdk's `agentrux login`).

2. In Dify, **Authorize** → **OAuth** tab
3. Fill `base_url` (`https://api.agentrux.com`) and `client_id` (= `dcr_<uuid>`). Leave `client_secret` blank for public PKCE clients
4. **Connect** → AgenTrux consent page → **Allow**
5. Dify stores the JWT and auto-rotates via `refresh_token` before expiry

### §B. `client_credentials` fallback ← **does not work in v1.1.0**

Intended for NAT-bound self-hosted Dify deployments that can't receive an OAuth callback. But v1.1.0 enforces a `script_` prefix on `client_id` while the server only issues `crd_<uuid>` — so this mode is currently broken. Use §A instead.

A prefix-check loosening is planned for the next patch release.

## Available tools (v1.1.0)

| Tool | Action | Required scope |
|---|---|---|
| `agentrux_publish` | Publish an event to a Topic | `topic.write` |
| `agentrux_read` | Read events from a Topic | `topic.read` |
| `agentrux_upload` | Upload binary payload to a Topic (max 15 MB) | `topic.write` |

`topic_id` is a dynamic-select drop-down populated from the JWT scope. `agentrux_publish` / `agentrux_upload` only list write-granted topics; `agentrux_read` only lists read-granted topics.

## Trigger events (v0.4.0)

| Event | Fires when | Output variables |
|---|---|---|
| `new_event` | A new event is published to a Topic the subscription is listening to | `event_id`, `sequence_number`, `event_type`, `topic_id`, `message`, `request_id`, `conversation_key`, `group_id`, `payload_json`, `metadata_json`, `attachment_urls` |

`event_type_filter` parameter (optional) restricts to a specific event_type (e.g. `composer.text` only).

## Legacy versions (Activation Code)

- `dify-agentrux-tools-0.2.x` / `0.3.x` are **deprecated**
- The legacy `/auth/activate` endpoint was replaced in server Phase 1.9 by `/auth/redeem-activation-code`; legacy plugins can't authenticate
- To migrate from v1.0.x to v1.1.0: **Delete the plugin → re-install v1.1.0 → Authorize**

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `client_id must start with 'script_'` | v1.1.0 client_credentials path mismatches server prefix (known issue above) | Switch to §A OAuth path |
| `unauthorized_client: client_id must be in 'crd_<uuid>' form` | Server requires `crd_` prefix | Issue a fresh Script Credential in Console |
| `AgenTrux rejected client_credentials (HTTP 401)` | `client_secret` mismatch / revoked | Rotate via Console |
| `OAuth state mismatch` | Callback URL changed after plugin process restart | Re-authorize |
| Topic does not appear in dynamic-select | No topic permission in JWT scope | Create a Grant in Console |
| Trigger does not invoke workflow | (1) Subscription created but not bound to a workflow, (2) `delivery_mode=webhook` but plugin endpoint not reachable from outside | (1) Confirm the workflow with the Trigger node is published, (2) For self-host, switch to `delivery_mode=sse` |

## For developers

- **Tools source**: [`src-1.1.0/`](./src-1.1.0/) — `provider/agentrux_tools.py` (OAuth methods), `provider/agentrux_api.py` (runtime), `tools/{publish,read,upload}.{py,yaml}`
- **Tools unit tests**: `tests/` (`pytest -x`)
- **Trigger source**: 0.4.0 ships only the `.difypkg`; the latest development source lives in the internal repository
- Minimum Dify: **1.10.0**
- Minimum dify_plugin SDK: **0.4.2**

## Related docs

- Auth contract: [AgenTrux API: OAuth 2.1](https://api.agentrux.com/.well-known/oauth-authorization-server)
