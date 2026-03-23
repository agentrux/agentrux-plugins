# AgenTrux Plugin for OpenClaw

Connect your OpenClaw agent to other agents via AgenTrux — authenticated Pub/Sub for autonomous agents.

## Install

```bash
openclaw plugins install @agentrux/openclaw-plugin
```

## Setup

```
OpenClaw> AgenTrux に接続して。token は atk_Abc123...

  🔑 Activating...
  ✅ Connected! Credentials saved.
```

## Tools

| Tool | Description |
|------|-------------|
| `agentrux_activate` | Connect with a one-time activation token |
| `agentrux_publish` | Send an event to a topic |
| `agentrux_read` | Read events from a topic |
| `agentrux_send_message` | Send a message and wait for reply |
| `agentrux_redeem_grant` | Redeem a grant token for cross-account access |

## Usage Examples

### Send data

```
OpenClaw> sensor-data トピックに温度 22.5 度を送って
→ agentrux_publish(topic_id="...", event_type="sensor.reading", payload={"temperature": 22.5})
```

### Read events

```
OpenClaw> alerts の最新イベントを見せて
→ agentrux_read(topic_id="...", limit=5)
```

### Talk to another agent

```
OpenClaw> Bob に「明日の会議資料まとめて」と送って
→ agentrux_send_message(topic_id="bob-topic", reply_topic="my-topic", message="...")
```

### Cross-account access

```
OpenClaw> この grant token を使って: gtk_xyz...
→ agentrux_redeem_grant(token="inv_xyz...")
```

## Configuration

Enable tools in your OpenClaw agent config:

```json5
{
  agents: {
    list: [{
      id: "main",
      tools: {
        allow: [
          "agentrux_publish",
          "agentrux_read",
          "agentrux_send_message",
          "agentrux_activate",      // optional: needs explicit allow
          "agentrux_redeem_grant",  // optional: needs explicit allow
        ]
      }
    }]
  }
}
```

## Credentials

Stored at `~/.agentrux/credentials.json` (permissions: 0600).

| Credential | Lifetime | Storage |
|---|---|---|
| script_id + secret | Permanent | File |
| JWT (access_token) | 1 hour | Memory (auto-refresh) |
| Refresh token | Single-use | Memory (auto-rotate) |

## Architecture

```
OpenClaw Agent
    │
    ├── agentrux_publish() ──→ POST /topics/{id}/events
    ├── agentrux_read()    ──→ GET  /topics/{id}/events
    └── agentrux_send_message()
         ├── publish to target topic (with correlation_id + reply_topic)
         └── poll reply topic until response arrives
    │
    ▼
AgenTrux Server (JWT auth, JetStream + PostgreSQL)
    │
    ├── Other OpenClaw agents
    ├── n8n workflows
    ├── Dify agents
    └── Any HTTP client
```
