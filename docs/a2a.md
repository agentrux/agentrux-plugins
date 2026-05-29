# AgenTrux A2A Agent Card

AgenTrux は [A2A (Agent-to-Agent) プロトコル](https://a2a-protocol.org/) の **Agent Card** を公開しています。他のエージェントはこの Agent Card を取得することで、AgenTrux が何をできるか (skills)、どこに接続するか (url / interfaces)、どう認証するか (security) を自動的に発見できます。

> English: AgenTrux publishes an A2A Agent Card so other agents can discover its skills, endpoints, and auth scheme.

## 取得方法

| 項目 | 値 |
|---|---|
| Agent Card URL | `https://api.agentrux.com/.well-known/agent-card.json` |
| Legacy alias | `https://api.agentrux.com/.well-known/agent.json` → `301` redirect |
| A2A protocol version | `0.3.0` |
| Content-Type | `application/json` (cache: `public, max-age=3600`) |

```bash
curl https://api.agentrux.com/.well-known/agent-card.json
```

`/.well-known/agent.json` は後方互換のため残してあり、`301` で `/.well-known/agent-card.json` にリダイレクトします。新規実装は `agent-card.json` を直接参照してください。

## Agent Card の構造

実際に返る主なフィールドは以下のとおりです。

| フィールド | 内容 |
|---|---|
| `protocolVersion` | `"0.3.0"` |
| `name` | `"AgenTrux"` |
| `description` | エージェント向けの短い説明 (認証付き短命 Pub/Sub) |
| `url` | API のベース URL (`https://api.agentrux.com`) |
| `version` | Agent Card の version (`"1.0.0"`) |
| `provider` | `{ "organization": "AgenTrux", "url": "<console URL>" }` |
| `documentationUrl` | `"https://docs.agentrux.com"` |
| `iconUrl` | アイコン (`.../static/icon.svg`) |
| `capabilities` | 下記参照 |
| `defaultInputModes` | `["application/json"]` |
| `defaultOutputModes` | `["application/json", "text/event-stream"]` |
| `supportedInterfaces` | 接続可能な interface (A2A / MCP) — 下記参照 |
| `securitySchemes` / `security` | 認証方式 — 下記参照 |
| `skills` | 提供スキル — 下記参照 |

### capabilities

```json
{
  "streaming": true,
  "pushNotifications": true,
  "stateTransitionHistory": false
}
```

### supportedInterfaces

AgenTrux は 2 つの protocol binding を公開しています。エージェントは自分の対応プロトコルに合う interface を選びます。

| `url` | `protocolBinding` | `protocolVersion` |
|---|---|---|
| `https://api.agentrux.com/a2a` | `HTTP+JSON` | `0.3.0` |
| `https://api.agentrux.com/mcp` | `MCP/Streamable-HTTP` | `2025-06-18` |

MCP 経由で接続する場合は [mcp.md](./mcp.md) を参照してください。

### securitySchemes / security

```json
"securitySchemes": {
  "bearer_jwt": { "type": "http", "scheme": "bearer", "bearerFormat": "JWT" }
},
"security": [{ "bearer_jwt": ["topic.read", "topic.write"] }]
```

認証は OAuth 2.1 で発行される Bearer JWT (access token) を使います。token の取得手順は Authorization Server Metadata から discovery できます (`https://api.agentrux.com/.well-known/oauth-authorization-server`)。

### skills

| `id` | `name` | 説明 | 必要 scope |
|---|---|---|---|
| `publish_event` | Publish Event | インライン JSON event (≤256 KiB) を Topic に publish | `topic.write` |
| `read_events` | Read Events | カーソルページネーション + `event_type` フィルタで Topic を読む | `topic.read` |
| `get_event` | Get Event | `event_id` で単一 event を取得 | `topic.read` |
| `list_topics` | List Topics | 接続中の workspace からアクセス可能な Topic 一覧 | — |
| `list_grants` | List Grants | 接続中の Script に紐づく Grant 一覧 | — |

各 skill は `tags` / `inputModes` / `outputModes` / `security` を持ちます (input/output はいずれも `application/json`)。

## 発見から利用までの流れ

```
1. GET /.well-known/agent-card.json
   → name / skills / supportedInterfaces / securitySchemes を取得

2. supportedInterfaces から接続先を選択 (A2A: /a2a, MCP: /mcp)

3. security から認証方式を確認 → OAuth 2.1 で access token を取得
   (GET /.well-known/oauth-authorization-server で endpoint を discovery)

4. 選んだ interface に Bearer JWT を付けて接続し、skill を呼び出す
```

## AgenTrux 拡張フィールド

A2A 仕様は拡張フィールドを許容しています。AgenTrux は `x_agentrux_*` 接頭語で以下を提供します。

- `x_agentrux_discovery` — OAuth / JWKS など各 well-known endpoint への URL 集約。
- `x_agentrux_error_codes` — API が返すエラーコード一覧 (`UNAUTHORIZED`, `FORBIDDEN`, `SUSPENDED`, `NOT_FOUND`, `CONFLICT`, `INVALID`, `PAYMENT_REQUIRED`, `PAYLOAD_TOO_LARGE`, `RATE_LIMITED`, `INTERNAL`)。
- `x_agentrux_support` — サポート窓口 (`email`, `status`)。

## 関連リンク

- Agent Card: `https://api.agentrux.com/.well-known/agent-card.json`
- MCP 接続: [mcp.md](./mcp.md)
- Authorization Server Metadata: `https://api.agentrux.com/.well-known/oauth-authorization-server`
- ドキュメント: `https://docs.agentrux.com`
