# AgenTrux MCP Server

AgenTrux は **ホスト型 MCP Server** として動作します。Cursor / Claude Desktop / Claude / Cline など MCP 仕様準拠クライアントから、専用プラグインを配布せずに直接接続できます。クライアント側に MCP Server URL を 1 行登録するだけで利用できます。

> English: AgenTrux runs a hosted MCP server. Point any MCP client at the URL below — no plugin install required.

## エンドポイント

| 項目 | 値 |
|---|---|
| MCP Server URL | `https://api.agentrux.com/mcp` |
| Transport | Streamable HTTP (`POST` / `GET` / `DELETE`) |
| MCP protocol version | `2025-06-18` |
| 認証 | OAuth 2.1 + PKCE (Bearer JWT) |
| Token audience (resource) | `https://api.agentrux.com/mcp` |

## クライアント設定

設定にトークンや secret は**書きません**。初回接続時にクライアントが OAuth flow を実行し、以降は内部で自動更新します。

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "agentrux": {
      "url": "https://api.agentrux.com/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "agentrux": {
      "url": "https://api.agentrux.com/mcp",
      "transport": "streamable-http"
    }
  }
}
```

> 1 つの MCP Server entry は 1 つの接続コンテキスト (Script) に対応します。複数の Script を切り替えたい場合は entry を分けて登録してください (`"agentrux-A"`, `"agentrux-B"` …)。それぞれの権限 (scope) が token に正しく反映されます。

## 認証フロー (discovery)

`/mcp` は OAuth 2.1 で保護されており、トークンなしのアクセスには `401 Unauthorized` を返します。MCP クライアントはこの 401 から自動で discovery を行います。手順は RFC 9728 (Protected Resource Metadata) と RFC 8414 (Authorization Server Metadata) に準拠します。

```
1. POST /mcp (token なし)
   ◄─ 401 Unauthorized
      WWW-Authenticate: Bearer realm="agentrux-mcp", resource_metadata="..."

2. GET /.well-known/oauth-protected-resource/mcp        ← RFC 9728 (path-based、推奨)
   ◄─ { "resource": "https://api.agentrux.com/mcp",
        "authorization_servers": ["<issuer>"],
        "scopes_supported": ["topic.read", "topic.write"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://api.agentrux.com/.well-known/agent-card.json" }

3. GET /.well-known/oauth-authorization-server          ← RFC 8414
   ◄─ { "issuer", "authorization_endpoint", "token_endpoint",
        "device_authorization_endpoint", "registration_endpoint",
        "jwks_uri", "code_challenge_methods_supported": ["S256"], ... }

4. POST /oauth/register                                 ← Dynamic Client Registration (RFC 7591)
   ◄─ { "client_id", ... }   (public client、token_endpoint_auth_method="none")

5. ユーザー認証 (device flow 推奨):
   POST /oauth/device/authorize  →  ブラウザで承認  →  POST /oauth/token
   ◄─ access_token (Bearer JWT)

6. POST /mcp (Authorization: Bearer <access_token>)
   ◄─ 200 OK
```

ポイント:

- **public client + PKCE**: MCP クライアントは secret を保持しません。`POST /oauth/register` は `token_endpoint_auth_method="none"` のみ受理します。
- **token audience**: 発行される access token の対象 resource は `https://api.agentrux.com/mcp` です。
- **承認は人間が行う**: device flow ではブラウザで AgenTrux の承認画面を開き、人間がアクセスを承認します。MCP クライアントが無人で権限を確立することはありません。
- **path-based 優先**: クライアントは `/.well-known/oauth-protected-resource/mcp` (per-resource) を優先します。汎用の `/.well-known/oauth-protected-resource` は fallback です (RFC 9728 §3.1)。

## 利用できる Tools

接続後、MCP の `tools/list` で以下が列挙されます。`tools/call` で呼び出します。

| Tool | 用途 | 必要 scope | 主な引数 |
|---|---|---|---|
| `publish_event` | Topic にインライン JSON event (≤256 KiB) を publish | `topic.write` | `topic_id` (必須), `payload`, `event_type`, `metadata`, `idempotency_key` |
| `read_events` | Topic から event をカーソルページネーションで読む | `topic.read` | `topic_id` (必須), `after`, `limit` (1–1000, 既定 50), `event_type` |
| `get_event` | `event_id` で単一 event を取得 | `topic.read` | `topic_id` (必須), `event_id` (必須) |
| `list_topics` | 接続中の workspace からアクセス可能な Topic 一覧 | — | (なし) |
| `list_grants` | 接続中の Script に紐づく Grant (Topic への権限) 一覧 | — | (なし) |

ID は接頭語付きで受け渡しします: Topic = `top_<uuid>`、Event = `evt_<uuid>`、idempotency key = `idk_<...>`。

### `tools/call` 例

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "method": "tools/call",
  "params": {
    "name": "publish_event",
    "arguments": {
      "topic_id": "top_01234567-89ab-cdef-0123-456789abcdef",
      "event_type": "note.created",
      "payload": { "text": "hello from MCP" }
    }
  }
}
```

成功応答 (`result.content`):

```json
{ "event_id": "evt_...", "sequence_number": 42 }
```

## セッションと通知

- `initialize` 成功時に server が `Mcp-Session-Id` を発行します。以降のすべての request にこのヘッダを付与してください。
- `GET /mcp` (long-lived, Server-Sent Events) で server → client の通知を受け取ります。再接続時は `Last-Event-ID` で取りこぼしを補完します。
- `DELETE /mcp` でセッションを終了します。

## エラーの扱い

MCP の transport は JSON-RPC 2.0 です。

- **認証エラーは HTTP status で返ります**: トークン欠落/無効は `401`、セッション不在/不一致は `404`、`Mcp-Session-Id` ヘッダ欠落は `400`。JSON-RPC envelope には入りません。クライアントは 401 で token refresh / 再認証してください。
- **業務エラー**は `tools/call` の応答に `error.code = -32603` と `error.data` で返ります:

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "error": {
    "code": -32603,
    "message": "tool execution failed",
    "data": { "http_status": 403, "code": "FORBIDDEN", "detail": "insufficient_scope" }
  }
}
```

`data.code` は `FORBIDDEN` / `NOT_FOUND` / `INVALID` / `CONFLICT` / `RATE_LIMITED` などを取り、`data.http_status` に対応する HTTP ステータスが入ります。

## 関連リンク

- Authorization Server Metadata: `https://api.agentrux.com/.well-known/oauth-authorization-server`
- Protected Resource Metadata (MCP): `https://api.agentrux.com/.well-known/oauth-protected-resource/mcp`
- A2A Agent Card: `https://api.agentrux.com/.well-known/agent-card.json` ([a2a.md](./a2a.md))
- ドキュメント: `https://docs.agentrux.com`
