# n8n-nodes-agentrux

n8n community node for [AgenTrux](https://github.com/agentrux/agentrux) — A2A authenticated ephemeral data pipe service.

## Installation

```bash
cd plugins/n8n
npm install
npm run build
```

### Option A: npm link (開発向け)

```bash
cd plugins/n8n && npm link
cd ~/.n8n && mkdir -p custom && cd custom && npm init -y && npm link n8n-nodes-agentrux
```

### Option B: Symlink

```bash
mkdir -p ~/.n8n/custom
ln -s "$(pwd)" ~/.n8n/custom/n8n-nodes-agentrux
```

### Option C: Docker

```yaml
services:
  n8n:
    image: n8nio/n8n
    volumes:
      - ./plugins/n8n:/home/node/custom-nodes/n8n-nodes-agentrux
    environment:
      - N8N_CUSTOM_EXTENSIONS=/home/node/custom-nodes/n8n-nodes-agentrux
```

n8n を再起動するとノードパネルに表示されます。

## Quick Start

### 1. Credential 作成（Activation Code モード）

| Field | Value |
|-------|-------|
| Base URL | `https://your-agentrux-server.example.com` |
| Auth Mode | **Activation Code (Initial Setup)** |
| Activation Code | `ac_...`（コンソールで発行したコード） |

「Test Credential」で接続確認 → Save

### 2. ノードを1回実行

AgenTrux ノードをキャンバスに配置して実行すると、**自動的に activate** されます。
出力の1件目に `script_id` と `client_secret` が含まれます:

```json
{
  "_setup": "AUTO_ACTIVATED",
  "script_id": "abc-123-...",
  "client_secret": "xxxxxxxxxxxxxxxx",
  "grants": [...]
}
```

### 3. Credential を切り替え（1回だけ）

| Field | Value |
|-------|-------|
| Auth Mode | **Script Credentials** |
| Script ID | 出力の `script_id` |
| Client Secret | 出力の `client_secret` |
| Invite Code | `inv_...`（任意、初回自動 redeem） |

以降はこの設定で動作し続けます。

## Nodes

### AgenTrux（Action Node）

**Resource: Topic**

| Operation | Description |
|-----------|-------------|
| Publish Event | イベントを topic に publish（correlation_id, reply_topic, payload_ref 対応） |
| Read Events | カーソルページネーションでイベント一覧取得 |
| Get Event | 単一イベントを ID で取得 |
| Upload Payload | バイナリデータを presigned URL 経由でアップロード |
| Download Payload | バイナリデータを presigned URL 経由でダウンロード |

**Resource: Auth**

| Operation | Description |
|-----------|-------------|
| Redeem Invite Code | `inv_...` コードを消費してクロスアカウントアクセスを取得 |

### AgenTrux Trigger

| Mode | Description |
|------|-------------|
| Polling | 定期的に `GET /topics/{id}/events` でカーソルベースのポーリング |
| Webhook | n8n の webhook URL を AgenTrux コンソールに登録 → ヒント通知を受信 |

Webhook モードでは HMAC-SHA256 署名検証に対応しています。

## Credentials

| Field | Mode | Required | Description |
|-------|------|----------|-------------|
| Base URL | 共通 | Yes | AgenTrux API サーバー URL |
| Auth Mode | 共通 | Yes | `Activation Code` / `Script Credentials` |
| Activation Code | Initial Setup | Yes | 初回 activate 用ワンタイムコード |
| Script ID | Script Credentials | Yes | スクリプト UUID |
| Client Secret | Script Credentials | Yes | スクリプトClient Secret |
| Invite Code | Script Credentials | No | クロスアカウント用（初回自動 redeem） |
| Webhook Secret | 共通 | No | Webhook HMAC-SHA256 署名検証用 |

## Authentication Flow

```
Activation Code mode                  Script Credentials mode
        │                                  │
        ▼                                  ▼
  POST /auth/activate             (invite code あり?)
        │                            │          │
        ▼                           Yes         No
  script_id + client_secret 取得           │          │
  (キャッシュ + 出力)                ▼          │
        │                   POST /auth/redeem   │
        │                   -grant (1回だけ)     │
        │                            │          │
        └────────────┬───────────────┘          │
                     ▼                          │
              POST /auth/token  ◄───────────────┘
                     │
                     ▼
               JWT キャッシュ (30s バッファ)
                     │
                     ▼
              POST /auth/refresh (期限切れ時)
                     │
                     ▼
              401 → キャッシュ破棄 → 再認証 (1回リトライ)
```

## License

MIT
