# agentrux-agent-tools

> **Beta** -- API may change before 1.0.

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

### 1. Create the toolkit

```python
import asyncio
from agentrux_agent_tools import AgenTruxToolkit

async def main():
    toolkit = await AgenTruxToolkit.create(
        base_url="https://api.example.com",
        script_id="your-script-id",
        client_secret="your-client-secret",
    )
    # Or use environment variables:
    # export AGENTRUX_BASE_URL=https://api.example.com
    # export AGENTRUX_SCRIPT_ID=...
    # export AGENTRUX_CLIENT_SECRET=...
    # toolkit = await AgenTruxToolkit.create()
```

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
| `AGENTRUX_INVITE_CODE` | Optional invite code for cross-account access |
