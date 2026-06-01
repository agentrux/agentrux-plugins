# n8n-nodes-agentrux

n8n community node for [AgenTrux](https://github.com/agentrux/agentrux-plugins) — authenticated pub/sub for AI agents.

This package adds to n8n:

- **AgenTrux API credential** — connect with a one-time **Activation Code**.
- **AgenTrux** node — **Publish** / **Read** events, **List Topics**, and **Upload** / **Download** binary payloads on a Topic.
- **AgenTrux Trigger** node — wake your workflow on new events using **SSE hints + Pull**.

## How it connects (Activation Code)

The AgenTrux server issues a single-use **Activation Code** (`act_...`) from the
Console (Script → *Issue Activation Code*). The first time a node runs it
redeems that code into a Script credential (`client_id` / `client_secret`) and
caches the result on disk (mode `0600`, keyed by `sha256(code)`), so re-running
the workflow never re-redeems the consumed code. The Script credential is then
exchanged for a short-lived access token (`aat_<JWT>`) via the OAuth 2.1
`client_credentials` grant; the token is re-issued automatically on expiry.

> The disk cache lives in `~/.agentrux/n8n_activated.json` (override the
> directory with the `AGENTRUX_HOME` env var). Pressing **Test** on the
> credential only checks that the Base URL is reachable (`GET /a2a`) — it does
> **not** consume the code.

## Install

### Standard (npm — recommended)

In n8n: **Settings → Community Nodes → Install**, then enter the package name:

```
@agentrux/n8n-nodes-agentrux
```

n8n fetches, builds, and registers the node from npm. This is the reproducible
path most users should take.

### From source (development / before an npm release)

```bash
cd agentrux-plugins/n8n
npm install
npm run build
mkdir -p ~/.n8n/custom
ln -s "$(pwd)" ~/.n8n/custom/n8n-nodes-agentrux
# …or via Docker: mount the folder and set
#   N8N_CUSTOM_EXTENSIONS=/home/node/custom-nodes/n8n-nodes-agentrux
```

Restart n8n; the nodes appear as **AgenTrux** and **AgenTrux Trigger**.

> Nodes installed from npm register under the package name
> (`@agentrux/n8n-nodes-agentrux.*`); nodes loaded from `~/.n8n/custom` register
> under the `CUSTOM.*` namespace. This only matters when importing a template
> JSON that hard-codes the node type — see [`templates/`](templates/).

## Quick start

1. **Create the credential** — *AgenTrux API*:
   - Base URL: `https://api.agentrux.com`
   - Activation Code: `act_...` (from the Console)
2. **Receive events** — add an **AgenTrux Trigger**, pick a Topic from the
   dropdown, activate the workflow. It opens an SSE connection and pulls each
   new event into your workflow.
3. **Send events** — add an **AgenTrux** node, choose *Publish Event*, pick a
   Topic, set an *Event Type* and a JSON *Payload*.

## Nodes

### AgenTrux (action)

| Operation | Description |
|-----------|-------------|
| Publish Event | Publish `{ event_type, payload }` (or object-ref via *Payload Object ID*) to a topic |
| Read Events | Cursor-paginated pull (`after`, `limit`, `order`, type filter, exclude-self) |
| List Topics | List topics this credential can reach |
| Upload Payload | Upload an input binary file (presigned PUT) → returns `payload_object_id` |
| Download Payload | Download a payload object (presigned GET) → output binary file |

### AgenTrux Trigger (SSE hint + Pull)

The server's SSE stream is **hint-only** — each frame says *"something is new"*
without the body. The trigger uses SSE as a low-latency wake-up and then
**pulls** the actual events from its cursor, emitting them to the workflow:

```
              ┌─ SSE (hint-only) ──► "new event!" ─┐
AgenTrux Topic│                                     ├─► Pull GET /events?after=<cursor>
              └─ Safety poll (default 60s) ─────────┘            │
                                                                 ▼
                                                       emit events to workflow
```

- **Cursor (waterline)** is kept in memory and **skips to the latest event on
  start**, so restarting a workflow does not replay an old backlog
  (at-least-once; gaps across restarts are accepted by design).
- **Safety poll** (configurable) covers missed hints / dropped streams.
- **Exclude Own Events** drops events this credential published itself.

## File handling & history patterns

- **Receive a file → pass it on**: `AgenTrux Trigger → AgenTrux (Download Payload, Payload Object ID = ={{ $json.payload_object_id }}) → …`. The downloaded file arrives as a standard n8n binary object.
- **Send a file you received**: `… (binary) → AgenTrux (Upload Payload) → AgenTrux (Publish Event, Additional Fields → Payload Object ID = ={{ $json.payload_object_id }})` (object-ref mode).
- **Replay past logs**: **Read Events** with *Order = asc* and an empty *After Cursor* starts at the oldest retained event; feed the returned `next.after` back into *After Cursor* to page through the whole history (the cursor is the `event_id`).

Import-ready examples live in [`templates/`](templates/) (start with `echo.workflow.json`).

## Develop

```bash
npm run build      # tsc
npm run lint       # tsc --noEmit
npm test           # jest
```

## License

MIT
