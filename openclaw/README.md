# AgenTrux Plugin for OpenClaw

Connect your OpenClaw agent to other agents via AgenTrux — authenticated Pub/Sub for autonomous agents.

- **Plugin version:** `1.0.22`
- **Target host:** OpenClaw **v8** (uses the v8 ChannelPlugin SDK pattern from `openclaw-nostr`)
- **Tested against:** AgenTrux production API (`https://api.agentrux.com`)

## Activation model

The channel activates by redeeming a **one-time activation code** (`act_...`)
through the `agentrux_activate` tool. On success the tool writes
`~/.agentrux/credentials.json` (the OAuth `client_id` / `client_secret`
issued for the Script); the gateway loads that file on every subsequent
start. The activation code is never written to `openclaw.json`, never
copied, never logged.

> **Retired:** earlier versions auto-activated from a `~/.agentrux/BOOTSTRAP.md`
> file on gateway start. That path is gone. If you still have a leftover
> `BOOTSTRAP.md`, the gateway renames it once to `BOOTSTRAP.md.legacy-<ts>`
> on the next start (so it stops tripping every restart) and logs why — it
> does not activate anything.

See the [Activation](#activation) section for the flow.

Transport hardening carried over from earlier releases:

- **Atomic credentials write** via tmp + rename — a crash mid-write can never leave a half-written `credentials.json`.
- **Single-flight gate** on JWT refresh — concurrent API calls coalesce onto one `/auth/refresh` instead of racing on the same single-use refresh token.
- **Compare-and-clear** on 401 in `authRequest()` — a stale 401 from a request that went out before another caller refreshed no longer clobbers the fresh cached token.
- Failed refreshes clear the dead refresh token instead of retrying it forever.
- Removed unused `ingressMode`, `webhookSecret`, and `activationCode` from `configSchema` — clean break, no backwards compatibility shim.

## Install

```bash
# From the local tarball that ships with this repo:
openclaw plugins install ./agentrux-openclaw-plugin-1.0.22.tgz

# Or, if it has been published to a registry:
openclaw plugins install @agentrux/openclaw-plugin@1.0.22

openclaw plugins list   # confirm: agentrux-openclaw-plugin (1.0.22) Format: openclaw
```

## Activation

### Step 1 — Configure topics in `openclaw.json` (one time)

The plugin needs three stable IDs in `openclaw.json` to know which topics to watch and which OpenClaw agent to dispatch to. Use `openclaw config set`:

```bash
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.commandTopicId "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.resultTopicId  "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.agentId        "<your-openclaw-agent-id>"
# optional, defaults to https://api.agentrux.com:
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.baseUrl "https://api.agentrux.com"
```

These three fields are stable identifiers, not secrets. They never change after initial setup.

### Step 2 — Issue an activation code

In the AgenTrux Console (web UI), go to the target Script and click **Issue Activation Code** (1h, 6h, or 24h TTL). It returns a one-time code that looks like `act_AbC123...`. Copy it now — the UI shows it exactly once.

### Step 3 — Redeem the code with `agentrux_activate`

Call the LLM-callable `agentrux_activate` tool with the code:

```
agentrux_activate(activation_code="act_AbC123...")
```

The tool calls `POST /auth/redeem-activation-code` exactly once, and on
success writes `~/.agentrux/credentials.json` (mode 0600) with the
`client_id` / `client_secret` issued for the Script. The activation code is
single-use and is never written to `openclaw.json` or persisted anywhere.

> An `agentrux_setup_via_device_code` tool (RFC 8628) also exists, but it
> writes a separate `device_credentials.json` that the gateway does **not**
> yet read. To activate this channel, use `agentrux_activate`.

### Step 4 — Start (or restart) the gateway

```bash
systemctl --user restart openclaw-gateway.service
# or however you run openclaw gateway
```

On start the gateway loads `~/.agentrux/credentials.json`. If it is present you should see:

```
[plugins] [agentrux] Registered as ChannelPlugin
[agentrux] Watching commandTopic <UUID>
```

If no credentials exist yet, the channel is disabled and the gateway logs:

```
[agentrux] No credentials at ~/.agentrux/credentials.json — channel disabled.
[agentrux] To activate: redeem a one-time activation code (act_...) with the `agentrux_activate` tool.
```

A leftover `BOOTSTRAP.md` from an older install no longer activates the channel. The gateway renames it once and logs:

```
[agentrux] BOOTSTRAP.md no longer activates this channel — moved to /home/<user>/.agentrux/BOOTSTRAP.md.legacy-20260529-134523.
```

### Re-activation

If credentials get lost, the script's secret is rotated by an admin, or you want to re-bind to a different script, issue a fresh activation code in the Console and redeem it again:

```bash
rm ~/.agentrux/credentials.json
# then call agentrux_activate(activation_code="act_NewCodeHere...")
systemctl --user restart openclaw-gateway.service
```

### Files under `~/.agentrux/`

| File | Purpose | Lifetime |
|---|---|---|
| `credentials.json` | `client_id` + `client_secret` (+ `script_id`) after successful activation. Mode 0600. | Until script secret is rotated (90 days) |
| `device_credentials.json` | Access/refresh tokens written by `agentrux_setup_via_device_code`. Not yet read by the gateway runtime. Mode 0600. | Until rotated |
| `waterline.json` | Per-topic read position for crash-safe Pull resume. Mode 0600. | Until manually deleted |
| `BOOTSTRAP.md.legacy-<ts>` | A retired `BOOTSTRAP.md` the gateway renamed aside. Inert — safe to delete. | Until you delete it |

## Configuration Reference

All keys live under `plugins.entries.agentrux-openclaw-plugin.config` in `openclaw.json`. Set them with `openclaw config set` (preferred — atomic and validated against the schema) or hand-edit the file. None of these are secrets, just stable identifiers.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `commandTopicId` | string | *required* | Topic ID to monitor for incoming commands |
| `resultTopicId` | string | *required* | Topic ID to publish results to |
| `agentId` | string | *required* | OpenClaw agent ID that processes commands |
| `baseUrl` | string | `https://api.agentrux.com` | AgenTrux API URL |
| `pollIntervalMs` | number | `60000` | Safety poller interval in ms (Pull fallback when SSE hints are missed) |
| `maxConcurrency` | number | `3` | Max concurrent subagent runs |
| `subagentTimeoutMs` | number | `120000` | Subagent timeout in ms |
| `execPolicy.enabled` | boolean | `false` | Enable exec tool for the ingress agent |
| `execPolicy.allowedCommands` | string[] | `[]` | Regex patterns for allowed commands |

> **Removed:** `activationCode` (redeem a one-time code with the `agentrux_activate` tool — see *Activation* above), `ingressMode` (was never read), `webhookSecret` (was never read).

## Credentials and tokens

| File | Purpose | Mode | Lifetime |
|---|---|---|---|
| `~/.agentrux/credentials.json` | `client_id` + `client_secret` + `base_url` (+ `script_id`). Written by `agentrux_activate` after a successful activation-code redemption, then read by the gateway on every subsequent start. | 0600 | 90 days (until the script secret is rotated) |
| `~/.agentrux/waterline.json` | Per-topic read position for crash-safe Pull resume | 0600 | Until manually deleted |

In-memory only (never persisted):

| Token | Lifetime | Notes |
|---|---|---|
| `access_token` (JWT) | 1h | Auto-refreshed via `/auth/refresh` or `/auth/token` |
| `refresh_token` | 24h, single-use | Rotated on every successful `/auth/refresh` |
| `activation_code` | 1–24h, single-use | Redeemed once by `agentrux_activate`, then forgotten |

The plugin uses a **single-flight gate** on token acquisition: concurrent API calls that all need a fresh token coalesce onto one `/auth/refresh` (or `/auth/token`) request. Without this, three concurrent calls would each issue their own `/auth/refresh` and two would lose the rotation race against the third, fall through to client_secret re-auth, and burn the rate limit. See [`http-client.ts`](src/http-client.ts) for the implementation.

If `/auth/refresh` returns 4xx (expired or revoked refresh token), the plugin clears the in-memory state and re-auths with `client_secret` on the next call. The dead refresh token is never retried.

`authRequest()` uses **compare-and-clear** on 401: it only invalidates the cached token state if the bearer token that just failed is still the cached one. Without this guard, a stale 401 from a request that went out before another caller refreshed would clobber the fresh token and force an unnecessary `/auth/token` round trip.

## Features

- **SSE hint + Pull drain**: SSE is used as a hint only; actual events are fetched via Pull API from the waterline, eliminating event loss on SSE disconnects
- **Inbound attachments**: Text files (≤50KB) inlined into the message; binary/large files passed as presigned URLs
- **Outbound attachments**: LLM can upload files via `agentrux_upload` tool; attachments auto-included in response
- **Per-topic waterline**: Persistent waterline per topic in `~/.agentrux/waterline.json` — crash-safe resume with no duplicates
- **Safety Poller**: Periodic Pull fallback (default 60s) in case SSE hints are missed
- **Two-layer dedup**: event_id (transport) + request_id (application)
- **ChannelPlugin**: Integrates with OpenClaw's reply pipeline for buffered block dispatch

## Tools (LLM-callable)

| Tool | Description |
|------|-------------|
| `agentrux_activate` | Connect with a one-time activation code (`act_...`); writes `credentials.json`. This is the primary activation path. |
| `agentrux_setup_via_device_code` | RFC 8628 device-code setup (writes `device_credentials.json`; not yet read by the gateway runtime). |
| `agentrux_publish` | Send an event to a topic |
| `agentrux_read` | Read events from a topic |
| `agentrux_send_message` | Send a message and wait for reply |
| `agentrux_redeem_grant` | Redeem an invite code for cross-Alias (cross-account) access |
| `agentrux_upload` | Upload a local file and get a download URL (auto-attaches to response during ingress) |

## Architecture

```
External Client                         OpenClaw Gateway
─────────────                           ────────────────
publish(commandTopic,                   ┌─ SSE (hint-only, no event body)
  {message: "check disk",              │     │
   attachments: [...]})                 │     ▼
     │                                  │  drainEvents() ─── Pull API (from waterline)
     ▼                                  │     │
AgenTrux Topic ─────────────────────────┘     │
                                        ├─ Safety Poller (60s fallback → drainEvents)
                                        │
                                        ▼
                                   Resolve inbound attachments (presigned URL → inline/ref)
                                        │
                                        ▼
                                   ChannelPlugin reply pipeline → LLM + Tools
                                        │
                                        ├─ agentrux_upload → pendingAttachments
                                        ▼
                                   deliver() → publish → Results Topic
                                        │         (text + attachments)
read(resultTopic) ←─────────────────────┘
```

**SSE hint + Pull drain**: SSE tells the plugin "there are new events" but does not carry the event body. The plugin then calls the Pull API starting from its saved waterline to fetch all new events. This decouples real-time notification from reliable delivery.

**Waterline scoping**: Each topic has its own waterline entry in `~/.agentrux/waterline.json`. On first startup the waterline is fast-forwarded to the latest event, so no old events are reprocessed.

## Sending commands from outside

```bash
curl -X POST "https://api.agentrux.com/topics/{commandTopicId}/events" \
  -H "Authorization: Bearer $JWT" \
  -d '{"type":"openclaw.request","payload":{"request_id":"req-001","message":"Check disk usage"}}'
```

OpenClaw processes the request using its LLM + tools (exec, browser, etc.) and publishes the result to `resultTopicId`.

### With attachments

```bash
curl -X POST "https://api.agentrux.com/topics/{commandTopicId}/events" \
  -H "Authorization: Bearer $JWT" \
  -d '{
    "type": "openclaw.request",
    "payload": {
      "request_id": "req-002",
      "message": "Analyze this log file",
      "attachments": [
        {"name": "error.log", "object_id": "obj_...", "content_type": "text/plain"}
      ]
    }
  }'
```

## Security

- `reply_topic` and `agent_id` fixed in config (not from request)
- Prompt injection mitigation via message template wrapping
- `sessionKey` hashed with topic scope
- `execPolicy`: exec tool disabled by default, opt-in with command allowlist
- Activation code never written to `openclaw.json` — redeemed once by `agentrux_activate`, then forgotten
- Credentials file is mode 0600

## License

MIT — see [LICENSE](./LICENSE). Full license text: <https://opensource.org/license/mit>.
