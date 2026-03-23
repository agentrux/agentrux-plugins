# flowise-node-agentrux

Flowise custom nodes for [AgenTrux](https://github.com/your-org/AgenTrux) - A2A authenticated ephemeral data pipe service.

**Status: Beta (0.1.0-beta.1)**

## Installation (Beta / Local Development)

```bash
cd plugins/flowise
npm install
npm run build
```

### Copy to Flowise components directory

```bash
# Find your Flowise installation
# Copy the built files to the components directory
cp -r dist/* /path/to/Flowise/packages/components/dist/nodes/agentrux/
```

Or add to Flowise via the `FLOWISE_COMPONENTS_PATH` environment variable:

```bash
export FLOWISE_COMPONENTS_PATH=/path/to/plugins/flowise/dist
```

Then restart Flowise. The nodes will appear in the node panel under the "AgenTrux" category.

## Nodes

### AgenTrux Publish
Publish an event to a topic. Accepts topic ID, event type, and JSON payload. Returns the `event_id` string.

### AgenTrux List Events
List events from a topic with cursor-based pagination and optional type filtering. Returns JSON with `events` array and `next_cursor`.

### AgenTrux Get Event
Retrieve a single event by ID. Returns the full event object as JSON.

### AgenTrux Payload (Upload/Download)
Two-step presigned URL flow for binary payloads:
- **Upload**: Creates a presigned URL via the API, then uploads data to it
- **Download**: Gets a presigned URL via the API, then downloads the content

## Credential

Configure the AgenTrux API credential with:

| Field | Required | Description |
|-------|----------|-------------|
| Base URL | Yes | AgenTrux server URL (e.g., `https://api.example.com`) |
| Script ID | Yes | UUID of the script to authenticate as |
| Client Secret | Yes | Client Secret |
| Invite Code | No | Cross-account invite code (redeemed on first use) |

## Authentication

All nodes share the same JWT lifecycle management:
1. Authenticates with script credentials via `POST /auth/token`
2. Caches the access token until near expiry (30s buffer)
3. Refreshes via `POST /auth/refresh` with token rotation
4. Falls back to full re-authentication if refresh fails
5. Retries once on 401 responses

## License

MIT
