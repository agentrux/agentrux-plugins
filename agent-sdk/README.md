# agentrux-agent-tools

> **Beta (0.3.1b1)** -- API may change before 1.0.

Framework-agnostic AI agent toolkit for
[AgenTrux](https://github.com/agentrux/agentrux).  Exposes
publish/subscribe/read operations as tool definitions compatible with OpenAI
function calling, Anthropic tool_use, and any other LLM framework.

## Installation

```bash
pip install agentrux-agent-tools
```

Or install from source:

```bash
cd plugins/agent-sdk
pip install -e .
```

## Quick Start

### 1a. Quick start (device flow, recommended for laptops / dev VMs)

```bash
pip install agentrux-agent-tools
agentrux login          # opens browser, completes OAuth 2.1 device flow,
                        # writes ~/.agentrux/credentials (INI)
```

Then in code:

```python
import asyncio
from agentrux_agent_tools import AgenTruxToolkit

async def main():
    # Reads ~/.agentrux/credentials and refreshes tokens automatically.
    toolkit = await AgenTruxToolkit.create()
```

### 1b. Headless / CI (client_credentials)

For unattended hosts where opening a browser is not possible, pass a Script's
`client_credentials` secret directly:

```python
async def main():
    toolkit = await AgenTruxToolkit.create(
        base_url="https://api.agentrux.com",
        script_id="your-script-id",
        client_secret="your-client-secret",
    )
    # Or use environment variables:
    # export AGENTRUX_BASE_URL=https://api.agentrux.com
    # export AGENTRUX_SCRIPT_ID=...
    # export AGENTRUX_CLIENT_SECRET=...
    # toolkit = await AgenTruxToolkit.create()
```

### Credentials file: `~/.agentrux/credentials`

`agentrux login` writes an INI file with the following per-profile fields:

| Field | Description |
|-------|-------------|
| `base_url` | AgenTrux API URL (e.g. `https://api.agentrux.com`) |
| `script_id` | Script identifier the token is bound to |
| `access_token` | Current OAuth 2.1 JWT |
| `refresh_token` | Refresh token (rotated on every `/oauth/token` call) |
| `expires_at` | epoch seconds, used to pre-emptively refresh |
| `client_id` | OAuth 2.1 client ID (`oauth-client_<uuid>`) |

The toolkit refreshes the bundle in-place via `POST /oauth/token`
(form-encoded, `grant_type=refresh_token`).

### Concurrent agents on the same machine

Multiple agent processes can share the same credentials file safely. Each
profile has a per-profile lockfile under `~/.agentrux/locks/<profile>.lock`,
so only one process writes a refreshed `TokenBundle` at a time and the others
re-read after the lock is released. No race on single-use refresh tokens.

### 2. Get tool definitions

```python
    # OpenAI format
    tools = toolkit.get_tools()

    # Anthropic format
    tools = toolkit.get_tools_anthropic()
```

### 3. Execute tool calls from the LLM

```python
    result = await toolkit.execute("publish_event", {
        "topic_id": "550e8400-e29b-41d4-a716-446655440000",
        "event_type": "chat.message",
        "payload": {"text": "Hello from the agent!"},
    })
    print(result)  # JSON string with event_id
```

## Usage with OpenAI

```python
import openai
from agentrux_agent_tools import AgenTruxToolkit

async def agent_loop():
    toolkit = await AgenTruxToolkit.create()
    client = openai.AsyncOpenAI()

    messages = [{"role": "user", "content": "Publish a greeting event"}]

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        tools=toolkit.get_tools(),
    )

    for tool_call in response.choices[0].message.tool_calls or []:
        import json
        args = json.loads(tool_call.function.arguments)
        result = await toolkit.execute(tool_call.function.name, args)
        messages.append(response.choices[0].message)
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        })

    # Continue the conversation with tool results...
    await toolkit.close()
```

## Usage with Claude (Anthropic)

```python
import anthropic
from agentrux_agent_tools import AgenTruxToolkit

async def agent_loop():
    toolkit = await AgenTruxToolkit.create()
    client = anthropic.AsyncAnthropic()

    response = await client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        tools=toolkit.get_tools_anthropic(),
        messages=[{"role": "user", "content": "List recent events from my topic"}],
    )

    for block in response.content:
        if block.type == "tool_use":
            result = await toolkit.execute(block.name, block.input)
            # Send result back as tool_result...

    await toolkit.close()
```

## Generic Agent Loop

```python
import json
from agentrux_agent_tools import AgenTruxToolkit

async def generic_agent(llm_call, user_prompt: str):
    """Works with any LLM that supports function calling."""
    async with await AgenTruxToolkit.create() as toolkit:
        tools = toolkit.get_tools()
        messages = [{"role": "user", "content": user_prompt}]

        while True:
            response = await llm_call(messages, tools=tools)

            if not response.tool_calls:
                return response.text

            for call in response.tool_calls:
                result = await toolkit.execute(call.name, call.arguments)
                messages.append({"role": "tool", "content": result})
```

## Available Tools

| Tool | Description |
|------|-------------|
| `publish_event` | Publish a JSON event to a topic. Returns the event_id. |
| `list_events` | List recent events with optional type filter. |
| `get_event` | Retrieve a single event by ID. |
| `wait_for_event` | Wait for the next event via SSE (with timeout). |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `AGENTRUX_BASE_URL` | Server URL |
| `AGENTRUX_SCRIPT_ID` | Script identifier |
| `AGENTRUX_CLIENT_SECRET` | Client Secret |
| `AGENTRUX_INVITE_CODE` | Optional invite code for cross-Domo (cross-account) access |
