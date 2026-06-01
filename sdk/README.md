# AgenTrux SDK

Python SDK for [AgenTrux](https://github.com/agentrux/agentrux-plugins) — A2A authenticated pub/sub client.

> **Status: Beta (0.4.0b2)** — OAuth 2.1 client_credentials + cursor-based pagination + SSE hint-only consumer.

## Install

```bash
pip install agentrux-sdk
```

The import name is `agentrux_sdk` (distinct from the server-internal
`agentrux` package, so the two never collide):

```python
from agentrux_sdk import AgentRuxClient
```

## Quick Start

```python
from agentrux_sdk import AgentRuxClient

# Confidential client (script credentials issued by Console)
async with AgentRuxClient(
    endpoint="https://api.agentrux.com",
    client_id="crd_<uuid>",        # script credential client_id
    client_secret="aks_<base64>",  # script credential client_secret
) as client:
    # --- Publish ------------------------------------------------------
    result = await client.publish(
        topic_id="top_<uuid>",
        payload={"msg": "Hello!"},
        event_type="hello.world",
    )
    print(result.event_id, result.sequence_number)

    # --- Read (cursor-based) ------------------------------------------
    async for evt in client.read_pull(topic_id="top_<uuid>"):
        print(evt.event_type, evt.payload)
```

## OAuth 2.1

`AgentRuxClient` uses the `client_credentials` grant. Pass the script
credentials (`client_id="crd_<uuid>"`, `client_secret="aks_<base64>"`)
issued by Console; the SDK obtains and re-issues access tokens
automatically — there is no refresh token to persist.

For interactive setup (device flow, RFC 8628) the approval step is
delegated to the Console SPA, not the SDK. See `install_topology` /
`device_code_setup` for the setup helpers.

## Read modes

- `read_hybrid(topic_id=...)`: pull-driven, SSE hints accelerate the next pull.
- `read_pull(topic_id=...)`: pull only; works without SSE reachability.
- `read_sse(topic_id=...)`: server-sent events with auto-reconnect.

Each returns an async iterator of `Event` (`.event_id`, `.event_type`,
`.payload`, `.sequence_number`). Use the last `event_id` as the `after=`
cursor to resume.

## Used by

This SDK is the shared transport layer for all AgenTrux Python plugins:
- `agentrux-agent-tools` — AI agent toolkit
- (Dify Tools / Dify Trigger — once migrated to v0.3)

## License

MIT — see [LICENSE](./LICENSE). Full license text: <https://opensource.org/license/mit>.
