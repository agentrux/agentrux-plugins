# AgenTrux MCP Server (Beta)

MCP (Model Context Protocol) server that wraps the AgenTrux SDK, allowing LLMs to publish and read events on AgenTrux topics.

> **Status:** Beta (`0.1.0-beta.1`). API may change.

## Installation

From the repository root:

```bash
pip install -e plugins/mcp
```

Or run directly with `uvx`:

```bash
uvx --from ./plugins/mcp agentrux-mcp
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AGENTRUX_BASE_URL` | Yes | Base URL of the AgenTrux server (e.g. `https://api.example.com`) |
| `AGENTRUX_SCRIPT_ID` | Yes | Script ID for authentication |
| `AGENTRUX_SECRET` | Yes | Script secret for authentication |
| `AGENTRUX_INVITE_CODE` | No | Grant token for cross-account topic access |

## Claude Desktop Configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentrux": {
      "command": "agentrux-mcp",
      "env": {
        "AGENTRUX_BASE_URL": "https://api.example.com",
        "AGENTRUX_SCRIPT_ID": "your-script-id",
        "AGENTRUX_SECRET": "your-secret"
      }
    }
  }
}
```

If installed via `pip install -e`, the `agentrux-mcp` command is available directly.
For `uvx`, use:

```json
{
  "mcpServers": {
    "agentrux": {
      "command": "uvx",
      "args": ["--from", "/path/to/AgenTrux/plugins/mcp", "agentrux-mcp"],
      "env": {
        "AGENTRUX_BASE_URL": "https://api.example.com",
        "AGENTRUX_SCRIPT_ID": "your-script-id",
        "AGENTRUX_SECRET": "your-secret"
      }
    }
  }
}
```

## Claude Code Configuration

Add to `.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "agentrux": {
      "command": "agentrux-mcp",
      "env": {
        "AGENTRUX_BASE_URL": "https://api.example.com",
        "AGENTRUX_SCRIPT_ID": "your-script-id",
        "AGENTRUX_SECRET": "your-secret"
      }
    }
  }
}
```

## Available Tools

### `publish_event`

Publish an event to a topic. Requires write permission.

**Parameters:**
- `topic_id` (string, required): UUID of the target topic
- `event_type` (string, required): Event type (e.g. `sensor.reading`)
- `payload` (object, optional): Inline JSON payload
- `payload_ref` (string, optional): Reference to an uploaded payload object

**Returns:** `{"event_id": "..."}`

### `list_events`

List events in a topic with pagination. Requires read permission.

**Parameters:**
- `topic_id` (string, required): UUID of the topic
- `limit` (integer, optional): Max events (1-100, default 50)
- `cursor` (string, optional): Pagination cursor
- `event_type` (string, optional): Filter by event type

**Returns:** `{"events": [...], "next_cursor": "..." | null}`

### `get_event`

Get a single event by ID. Requires read permission.

**Parameters:**
- `topic_id` (string, required): UUID of the topic
- `event_id` (string, required): UUID of the event

**Returns:** Event data object

### `get_upload_url`

Get a presigned URL for uploading a large payload to MinIO storage. Use before publishing an event with `payload_ref`.

**Parameters:**
- `topic_id` (string, required): UUID of the topic
- `size` (integer, required): Payload size in bytes
- `content_type` (string, optional): MIME type (default: `application/octet-stream`)
- `hash` (string, optional): SHA-256 hash for integrity verification

**Returns:** `{"object_id": "...", "upload_url": "...", "expiration": "..."}`

### `get_download_url`

Get a presigned URL for downloading a payload from MinIO storage.

**Parameters:**
- `topic_id` (string, required): UUID of the topic
- `object_id` (string, required): UUID of the payload object

**Returns:** `{"object_id": "...", "content_type": "...", "size": ..., "download_url": "...", "expiration": "..."}`

## Available Resources

### `agentrux://accessible-topics`

Lists all topics the authenticated script has access to, parsed from the JWT scope claim. Returns an array of `{"topic_id": "...", "action": "..."}` objects.

### `agentrux://topics/{topic_id}/events`

Dynamic resource that returns recent events for a specific topic (up to 20 most recent).

## Architecture

```
MCP Client (Claude, etc.)
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

The MCP server authenticates on first tool/resource call using `AGENTRUX_SCRIPT_ID` and `AGENTRUX_SECRET` to obtain a JWT. Token refresh is handled automatically by the SDK.
