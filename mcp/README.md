# AgenTrux MCP Server

MCP (Model Context Protocol) server that lets LLMs publish and read events on AgenTrux topics. Works with Claude Desktop, Claude Code, Cursor, and any MCP-compatible client.

**Status:** Beta (`0.1.0-beta.1`)

## Quick Start

### 1. Install

```bash
pip install agentrux-mcp
```

### 2. Get credentials

Create a Script in the AgenTrux Console and activate it to obtain:
- **Script ID** (UUID)
- **Client Secret**

### 3. Configure your MCP client

**Claude Desktop** — add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentrux": {
      "command": "agentrux-mcp",
      "env": {
        "AGENTRUX_BASE_URL": "https://api.agentrux.com",
        "AGENTRUX_SCRIPT_ID": "your-script-id",
        "AGENTRUX_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

**Claude Code** — add to `.mcp.json` in project root:

```json
{
  "mcpServers": {
    "agentrux": {
      "command": "agentrux-mcp",
      "env": {
        "AGENTRUX_BASE_URL": "https://api.agentrux.com",
        "AGENTRUX_SCRIPT_ID": "your-script-id",
        "AGENTRUX_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

### 4. Use from your LLM

```
You: "List events on my sensor topic"
LLM: [calls list_events tool]

You: "Publish a command to run the daily report"
LLM: [calls publish_event tool]
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `AGENTRUX_BASE_URL` | Yes | AgenTrux server URL (e.g. `https://api.agentrux.com`) |
| `AGENTRUX_SCRIPT_ID` | Yes | Script ID for authentication |
| `AGENTRUX_CLIENT_SECRET` | Yes | Client secret for authentication |
| `AGENTRUX_INVITE_CODE` | No | Invite code for cross-account topic access |

## Tools

### publish_event

Publishes an event to a topic. Requires write permission.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic_id` | string (UUID) | Yes | Target topic |
| `event_type` | string | Yes | Event type (e.g. `sensor.reading`) |
| `payload` | object | No | JSON payload |
| `payload_ref` | string | No | Reference to an uploaded payload object |

Returns: `{"event_id": "..."}`

### list_events

Lists events with cursor pagination. Requires read permission.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic_id` | string (UUID) | Yes | Topic |
| `limit` | integer | No | Max events (1-100, default 50) |
| `cursor` | string | No | Pagination cursor from previous response |
| `event_type` | string | No | Filter by event type |

Returns: `{"events": [...], "next_cursor": "..." | null}`

### get_event

Fetches a single event. Requires read permission.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic_id` | string (UUID) | Yes | Topic |
| `event_id` | string (UUID) | Yes | Event |

### get_upload_url

Gets a presigned URL for uploading large files. Use before publishing with `payload_ref`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic_id` | string (UUID) | Yes | Topic |
| `size` | integer | Yes | File size in bytes |
| `content_type` | string | No | MIME type (default: `application/octet-stream`) |
| `hash` | string | No | SHA-256 hash for integrity |

Returns: `{"object_id": "...", "upload_url": "...", "expiration": "..."}`

### get_download_url

Gets a presigned URL for downloading a file.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic_id` | string (UUID) | Yes | Topic |
| `object_id` | string (UUID) | Yes | Payload object |

Returns: `{"object_id": "...", "content_type": "...", "size": ..., "download_url": "...", "expiration": "..."}`

## Resources

### agentrux://accessible-topics

Lists topics accessible to the authenticated script (from JWT scope).

Returns: `[{"topic_id": "...", "action": "read|write|read+write"}, ...]`

### agentrux://topics/{topic_id}/events

Recent events for a topic (up to 20).

## Architecture

```
MCP Client (Claude, Cursor, etc.)
    |
    | stdio (JSON-RPC)
    v
agentrux-mcp (this server)
    |
    | HTTPS (JWT auth)
    v
AgenTrux API Server
    |
    +-> JetStream (events)
    +-> MinIO (large payloads)
    +-> PostgreSQL (metadata)
```

Authentication happens on the first tool or resource call. Token refresh is automatic.

## Use Cases

### Remote agent control

Send commands to OpenClaw, n8n, or other agents via AgenTrux topics, and read their responses.

```
Claude --> publish_event(commandTopic, "run.report") --> Agent
Claude <-- list_events(resultTopic)                  <-- Agent response
```

### File transfer

Upload and download files through presigned URLs:

```
1. get_upload_url  -->  PUT file to presigned URL
2. publish_event(payload_ref=object_id)
3. list_events     -->  get_download_url  -->  GET file
```

### Cross-agent collaboration

Read events from a shared workspace topic where multiple agents post updates. Use an invite code to access topics from other accounts.

## License

MIT
