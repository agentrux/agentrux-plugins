# AgenTrux Plugin for OpenClaw

Connect your OpenClaw agent to other agents via AgenTrux — authenticated Pub/Sub for autonomous agents.

- **Plugin version:** `0.7.4`
- **Target host:** OpenClaw **v8** (uses the v8 ChannelPlugin SDK pattern from `openclaw-nostr`)
- **Tested against:** AgenTrux production API (`https://api.agentrux.com`)

## What is in 0.7.4

Activation moved from a permanent config field to a one-shot **`BOOTSTRAP.md` ritual**, mirroring OpenClaw's own [agent bootstrap pattern](https://docs.openclaw.ai/start/bootstrapping). You drop the activation code into `~/.agentrux/BOOTSTRAP.md` once, restart the gateway, and the file is consumed and deleted automatically. The activation code is never written to `openclaw.json`, never copied, never logged.

See the [Activation](#activation) section for the new flow.

Other 0.7.4 changes:

- **Atomic claim** on the bootstrap file via POSIX `rename(2)` — concurrent gateway starts cannot both call `/auth/activate` on the same single-use code. See *Concurrency model* below.
- **Atomic credentials write** via tmp + rename — a crash mid-write can never leave a half-written `credentials.json`.
- **Single-flight gate** on JWT refresh — concurrent API calls now coalesce onto one `/auth/refresh` instead of racing on the same single-use refresh token.
- **Compare-and-clear** on 401 in `authRequest()` — a stale 401 from a request that went out before another caller refreshed no longer clobbers the fresh cached token.
- Failed refreshes now clear the dead refresh token instead of retrying it forever.
- Distinguishable `creds-already-present` quarantine outcome — the gateway logs specifically when a `BOOTSTRAP.md` is found alongside live credentials (instead of silently no-op'ing).
- Removed unused `ingressMode` and `webhookSecret` from `configSchema` (they were declared but never read by the runtime).
- Removed `activationCode` from `configSchema` — clean break, no backwards compatibility shim. Old configs that still set it will be ignored.

All bug fixes have unit-test coverage (77 cases pinning the contract) and were verified on a real VM, including a full Spot preemption + recovery cycle.

## Install

```bash
# From the local tarball that ships with this repo:
openclaw plugins install ./agentrux-agentrux-openclaw-plugin-0.7.4.tgz

# Or, if it has been published to a registry:
openclaw plugins install @agentrux/agentrux-openclaw-plugin@0.7.4

openclaw plugins list   # confirm: agentrux-openclaw-plugin (0.7.4) Format: openclaw
```

## Activation

This is a **one-shot ritual**. You will not edit `openclaw.json` by hand and you will not copy the activation code into a config file.

### Step 1 — Configure topics in `openclaw.json` (one time)

The plugin still needs three stable IDs in `openclaw.json` to know which topics to watch and which OpenClaw agent to dispatch to. Use `openclaw config set`:

```bash
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.commandTopicId "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.resultTopicId  "<UUID>"
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.agentId        "<your-openclaw-agent-id>"
# optional, defaults to https://api.agentrux.com:
openclaw config set plugins.entries.agentrux-openclaw-plugin.config.baseUrl "https://api.agentrux.com"
```

These three fields are stable identifiers, not secrets. They never change after initial setup.

### Step 2 — Issue an activation code

In the AgenTrux Console (web UI), go to the target Script and click **Issue Activation Code** (1h, 6h, or 24h TTL). The Console calls:

```
POST /console/scripts/{script_id}/activation-codes
```

and returns a one-time code that looks like `ac_AbC123...`. Copy it now — the UI shows it exactly once.

### Step 3 — Drop the code into `~/.agentrux/BOOTSTRAP.md`

```bash
mkdir -p ~/.agentrux && chmod 700 ~/.agentrux
( umask 077 && cat > ~/.agentrux/BOOTSTRAP.md ) <<EOF
# AgenTrux activation
# This file is consumed and deleted on the next gateway start.

ac_AbC123...
EOF
```

The file format is permissive: any line starting with `ac_` is treated as the activation code. You can add markdown commentary above and below it.

> **Why a file and not a config field?** OpenClaw's own agent bootstrap uses `~/.openclaw/workspace/BOOTSTRAP.md` for exactly the same reason: a single-use, time-limited secret does not belong in a permanent config file. Files can be deleted on consumption; config fields cannot. See [OpenClaw bootstrapping docs](https://docs.openclaw.ai/start/bootstrapping).

### Step 4 — Start (or restart) the gateway

```bash
systemctl --user restart openclaw-gateway.service
# or however you run openclaw gateway
```

What happens on first start:

1. Gateway sees no `~/.agentrux/credentials.json`.
2. Gateway claims `~/.agentrux/BOOTSTRAP.md` by atomically renaming it to `BOOTSTRAP.md.inflight`. If two gateway processes race, exactly one wins this rename and the other treats the file as already gone. This is what makes the single-use code safe under concurrent startup.
3. Gateway reads the activation code from the inflight file and calls `{baseUrl}/auth/activate` exactly once.
4. **On success:** writes `~/.agentrux/credentials.json` (mode 0600) and **deletes** the inflight file. The ritual is over.
5. **On permanent failure (4xx):** renames the inflight file to `BOOTSTRAP.md.failed-<timestamp>` and writes `BOOTSTRAP.md.failed-<timestamp>.json` with diagnostics. The original file is gone, so OpenClaw's auto-restart loop cannot retry the dead code.
6. **On transient failure (5xx / network):** renames the inflight file *back* to `BOOTSTRAP.md` and throws, so the next auto-restart attempt picks it up again.

In the logs you should see one of:

```
[plugins] [agentrux] Registered as ChannelPlugin
[agentrux] Activated via /home/<user>/.agentrux/BOOTSTRAP.md: script_id=scr_...
[agentrux] Watching commandTopic <UUID>
```

or

```
[agentrux] BOOTSTRAP.md activation rejected (HTTP 422 INVALID): Token has expired
[agentrux] Quarantined to /home/<user>/.agentrux/BOOTSTRAP.md.failed-20260408-134523. Issue a new activation code...
```

or, if you forgot Step 3:

```
[agentrux] No credentials at ~/.agentrux/credentials.json — channel disabled.
[agentrux] To activate: write your one-time activation code to /home/<user>/.agentrux/BOOTSTRAP.md and restart the gateway.
```

### Re-activation

If credentials get lost, the script's secret is rotated by an admin, or you want to re-bind to a different script:

```bash
rm ~/.agentrux/credentials.json
( umask 077 && cat > ~/.agentrux/BOOTSTRAP.md ) <<EOF
ac_NewCodeHere...
EOF
systemctl --user restart openclaw-gateway.service
```

### Files under `~/.agentrux/`

| File | Purpose | Lifetime |
|---|---|---|
| `BOOTSTRAP.md` | Pending activation. Consumed and deleted on next gateway start. | One-shot |
| `BOOTSTRAP.md.inflight` | The plugin is mid-claim. Should not be touched while the gateway is running. If this file is left behind after a hard crash with no `BOOTSTRAP.md` next to it, see *Recovering from an orphaned inflight file* below. | Transient |
| `credentials.json` | `script_id` + `client_secret` after successful activation. Mode 0600. | Until script secret is rotated (90 days) |
| `BOOTSTRAP.md.failed-<ts>` | Quarantined bootstrap file from a permanent failure. Safe to delete. | Until you delete it |
| `BOOTSTRAP.md.failed-<ts>.json` | Diagnostic sidecar for the corresponding `.failed-<ts>` file. | Until you delete it |
| `waterline.json` | Per-topic read position for crash-safe Pull resume. Mode 0600. | Until manually deleted |

### Concurrency model: atomic claim

The bootstrap ritual uses an **atomic rename** to claim ownership of the activation attempt. When the gateway starts and finds a `BOOTSTRAP.md`, it tries to `rename(BOOTSTRAP.md → BOOTSTRAP.md.inflight)` *before* making any network call. POSIX `rename(2)` is atomic, so if two gateway processes race (Spot preemption recovery, systemd restart overlapping a manual start), exactly one wins the rename and the others see `ENOENT` and treat the situation as "no file". Only the winner ever calls `/auth/activate`. This guarantees that a single-use activation code cannot be burned twice.

State transitions of the inflight file:

- **200 success** → inflight file is `unlink`ed. The ritual is over.
- **4xx permanent failure** → inflight file is renamed to `BOOTSTRAP.md.failed-<ts>` with a `.json` sidecar. The original `BOOTSTRAP.md` is already gone, so OpenClaw's auto-restart loop sees no file and stays quiet (no rate-limit burn).
- **5xx / network transient failure** → inflight file is renamed *back* to `BOOTSTRAP.md` and the error is thrown. The next auto-restart attempt picks it up.

### Recovering from an orphaned inflight file

If the gateway is killed at exactly the wrong moment (between the rename and the `unlink`/rename-back), you may end up with `BOOTSTRAP.md.inflight` and no `BOOTSTRAP.md`. The plugin will NOT auto-recover this — earlier drafts tried, and the recovery branch was the source of a concurrent race that could burn the single-use code on a second `/auth/activate` call. Manual recovery is one command:

```bash
mv ~/.agentrux/BOOTSTRAP.md.inflight ~/.agentrux/BOOTSTRAP.md
systemctl --user restart openclaw-gateway.service
```

If `credentials.json` is also present and the inflight file is older than your last successful start, you can simply delete the inflight file: it is no longer needed.

### Safety guard: BOOTSTRAP.md plus existing credentials

If `BOOTSTRAP.md` exists **and** `credentials.json` already exists, the gateway will NOT call `/auth/activate`. Instead it quarantines the bootstrap file (renaming it to `BOOTSTRAP.md.failed-<ts>` with a sidecar explaining "credentials.json already exists; refusing to consume"). This prevents the most expensive mistake — burning a fresh single-use code on top of working credentials. Delete `credentials.json` first if you really want to re-activate.

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

> **Removed in 0.7.4:** `activationCode` (use `~/.agentrux/BOOTSTRAP.md` — see *Activation* above), `ingressMode` (was never read), `webhookSecret` (was never read).

## Credentials and tokens

| File | Purpose | Mode | Lifetime |
|---|---|---|---|
| `~/.agentrux/credentials.json` | `script_id` + `client_secret` + `base_url`. Written by the gateway after a successful BOOTSTRAP.md activation, then read by the gateway on every subsequent start. | 0600 | 90 days (until the script secret is rotated) |
| `~/.agentrux/waterline.json` | Per-topic read position for crash-safe Pull resume | 0600 | Until manually deleted |

In-memory only (never persisted):

| Token | Lifetime | Notes |
|---|---|---|
| `access_token` (JWT) | 1h | Auto-refreshed via `/auth/refresh` or `/auth/token` |
| `refresh_token` | 24h, single-use | Rotated on every successful `/auth/refresh` |
| `activation_code` | 1–24h, single-use | Consumed by the BOOTSTRAP.md ritual, then forgotten |

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
| `agentrux_activate` | Connect with a one-time activation code (legacy LLM-callable path; the recommended flow is the BOOTSTRAP.md ritual described above) |
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
- Activation code never written to `openclaw.json` — consumed in TTY only
- Credentials file is mode 0600
