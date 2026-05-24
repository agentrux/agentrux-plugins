# AgenTrux SDK

Python SDK for [AgenTrux](https://github.com/agentrux/agentrux-plugins) — A2A authenticated pub/sub client.

> **Status: Beta (0.3.0b1)** — OAuth 2.1 (Phase 1.9+) + cursor-based pagination + SSE hint-only consumer.

## Install

```bash
pip install agentrux-sdk
```

## Quick Start

```python
from agentrux.sdk import AgenTruxClient

# 1) Confidential client (script credentials issued by Console)
client = await AgenTruxClient.from_client_credentials(
    "https://api.agentrux.com",
    client_id="crd_<uuid>",        # script credential client_id
    client_secret="aks_<base64>",  # script credential client_secret
)

# 2) Activation code (one-shot redemption → persisted client_credentials)
client = await AgenTruxClient.from_activation_code(
    "https://api.agentrux.com",
    activation_code="act_<base64>",
    save_credentials_to="~/.agentrux/credentials.json",
)

# 3) Public client via device flow (RFC 8628 + DCR)
dcr = await AgenTruxClient.register_dcr_client(
    "https://api.agentrux.com", client_name="my-plugin",
)
auth = await AgenTruxClient.start_device_flow(
    "https://api.agentrux.com", oauth_client_id=dcr.client_id,
    scope="topic:abc:read topic:abc:write",
)
print(f"User: visit {auth.verification_uri_complete} and approve.")
client = await AgenTruxClient.complete_device_flow(
    "https://api.agentrux.com",
    device_code=auth.device_code,
    oauth_client_id=dcr.client_id,
)

# --- Publish & subscribe ----------------------------------------------

await client.publish("top_<uuid>", "hello.world", {"msg": "Hello!"})

async with client.subscribe("top_<uuid>") as sub:
    async for envelope in sub:
        print(envelope.event_type, envelope.payload)
```

## OAuth 2.1

| Factory | Grant | Refresh |
|---------|-------|---------|
| `from_client_credentials(client_id="crd_<uuid>", client_secret=...)` | `client_credentials` | automatic re-issue (no refresh_token; the SDK re-runs the credential exchange) |
| `from_activation_code(activation_code="act_<base64>")` | redeems AC → `client_credentials` | same as above |
| `complete_device_flow(device_code=..., oauth_client_id="dcr_<uuid>")` | `device_code` → access+refresh | `OAuthRefreshTokenRefresher` (RFC 6749 §6) |
| `from_access_token(access_token=..., refresh_token=..., oauth_client_id=...)` | bring-your-own | wired automatically when `oauth_client_id` is provided |

`on_token_refreshed: Callable[[TokenBundle], None]` is invoked after each
successful refresh; use it to persist the rotated tokens.

### `TokenBundle` dataclass

```python
@dataclass(frozen=True)
class TokenBundle:
    access_token: str
    refresh_token: str | None     # client_credentials path → None
    expires_at_unix: int          # absolute unix epoch seconds
```

### Persistence example

```python
def save(bundle: TokenBundle) -> None:
    Path("~/.agentrux/credentials").expanduser().write_text(
        f"access_token={bundle.access_token}\n"
        f"refresh_token={bundle.refresh_token}\n"
        f"expires_at_unix={bundle.expires_at_unix}\n"
    )

client = await AgenTruxClient.from_access_token(
    "https://api.agentrux.com",
    access_token=loaded.access_token,
    refresh_token=loaded.refresh_token,
    oauth_client_id="dcr_<uuid>",
    on_token_refreshed=save,
)
```

## Subscribe modes

- `mode="hybrid"` (default): pull-driven, SSE hints accelerate the next pull.
- `mode="pull"`: pull only; works without SSE reachability.
- `mode="sse"`: alias for `hybrid` with SSE enabled (kept for naming clarity).

Pass `on_resync_required=` to be notified when the server emits
`event: resync_required` (the cursor became invalid — reset checkpoint
and resubscribe). The hybrid consumer **always** surfaces a
`ResyncRequiredError` to the iterator after the callback returns; the
callback itself is for logging/metrics only.

## Used by

This SDK is the shared transport layer for all AgenTrux Python plugins:
- `agentrux-agent-tools` — AI agent toolkit
- (Dify Tools / Dify Trigger — once migrated to v0.3)

## License

MIT
