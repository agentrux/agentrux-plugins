# AgenTrux SDK

Python SDK for [AgenTrux](https://github.com/agentrux/agentrux-plugins) — A2A authenticated pub/sub client.

> **Status: Beta (0.1.0b1)**

## Install

```bash
pip install agentrux-sdk
```

## Quick Start

```python
from agentrux.sdk.facade import AgenTruxClient

client = await AgenTruxClient.connect(
    base_url="https://api.agentrux.com",
    script_id="your-script-id",
    client_secret="your-client-secret",
)

# Publish
await client.publish("topic-uuid", "hello.world", {"msg": "Hello!"})

# Subscribe
async for envelope in client.subscribe("topic-uuid"):
    print(envelope.payload)
```

## Used by

This SDK is the shared transport layer for all AgenTrux Python plugins:
- `agentrux-agent-tools` — AI agent toolkit
- `agentrux-mcp` — MCP server
- `langflow-agentrux` — Langflow components
- `temporal-agentrux` — Temporal activities

## License

MIT
