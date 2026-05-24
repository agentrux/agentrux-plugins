# AgenTrux Plugin for OpenClaw

Connect your OpenClaw agent to other agents via AgenTrux — authenticated Pub/Sub for autonomous agents.

- **Plugin version:** `0.15.0`
- **Target host:** OpenClaw **v8** (uses the v8 ChannelPlugin SDK pattern from `openclaw-nostr`)
- **Tested against:** AgenTrux production API (`https://api.agentrux.com`)

## Setup

This plugin authenticates against AgenTrux using OAuth 2.1 `client_credentials`
(RFC 6749 §4.4). There is no activation code, no bootstrap file, and no
refresh token — every call uses the Script's `client_secret` directly to obtain
a short-lived access token.

1. **Provision a Script in AgenTrux Console.** Create a Script, attach the
   topics you need (`commandTopicId`, `resultTopicId`), and copy the
   `client_secret`. The secret is shown exactly once.
2. **Write `~/.agentrux/credentials.json`** (mode 0600) on the host that runs
   OpenClaw:
   ```json
   {
     "base_url": "https://api.agentrux.com",
     "script_id": "scr_<uuid>",
     "clientSecret": "wh07tr3I..."
   }
   ```
3. **Install the plugin:**
   ```bash
   npm install -g @agentrux/agentrux-openclaw-plugin@^0.15
   openclaw plugins list   # confirm: agentrux-openclaw-plugin (0.15.0)
   ```
4. **Token acquisition.** The plugin calls
   `POST /oauth/token` form-encoded with `grant_type=client_credentials`
   (RFC 6749 §4.4). Per RFC §4.4.3 a refresh token is **not** issued for this
   grant — the plugin simply re-acquires when the access token expires.
5. **Legacy `BOOTSTRAP.md` files are auto-quarantined on startup** with an
   explicit log line. If you upgrade an old install, the gateway will rename
   any leftover `~/.agentrux/BOOTSTRAP.md` to `BOOTSTRAP.md.failed-<ts>` and
   continue with the OAuth 2.1 flow.

### Configure topics in `openclaw.json`

The plugin still needs three stable IDs to know which topics to watch and
which OpenClaw agent to dispatch to. Use `openclaw config set`:

```bash
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.commandTopicId "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.resultTopicId  "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.agentId        "<your-openclaw-agent-id>"
# optional, defaults to https://api.agentrux.com:
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.baseUrl "https://api.agentrux.com"
```

These three fields are stable identifiers, not secrets. They never change
after initial setup.

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

## Credentials and tokens

| File | Purpose | Mode | Lifetime |
|---|---|---|---|
| `~/.agentrux/credentials.json` | `script_id` + `clientSecret` + `base_url`. Provisioned by the operator (see *Setup*). | 0600 | Until the Script secret is rotated |
| `~/.agentrux/waterline.json` | Per-topic read position for crash-safe Pull resume | 0600 | Until manually deleted |

In-memory only (never persisted):

| Token | Lifetime | Notes |
|---|---|---|
| `access_token` (JWT) | 1h | Re-acquired via `POST /oauth/token` (`grant_type=client_credentials`) on expiry |

The plugin uses a **single-flight gate** on token acquisition: concurrent API
calls that all need a fresh token coalesce onto one `/oauth/token` request,
so we never burn the rate limit with redundant client_credentials grants.

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
| `agentrux_publish` | Send an event to a topic |
| `agentrux_read` | Read events from a topic |
| `agentrux_send_message` | Send a message and wait for reply |
| `agentrux_redeem_grant` | Redeem an invite code for cross-Domo (cross-account) access |
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
- `client_secret` is read from `~/.agentrux/credentials.json` (mode 0600); never written to `openclaw.json` and never logged
- Legacy `BOOTSTRAP.md` files are quarantined on startup so a stale single-use code can never be replayed against the new OAuth 2.1 endpoint
