# AgenTrux Plugins

AgenTrux を外部プラットフォームから使うためのプラグイン集。

> **Status: Beta** — API は変更される可能性があります。

**初めての方:** まず [セットアップガイド](https://app.agentrux.com/docs/setup) で Alias の作成、および Topic・Script・Grant の作成を行い、その後 `agentrux login`（device flow）または Console から OAuth Client を登録してください。

## プラグイン一覧

| Plugin | Registry | Install | Status |
|--------|----------|---------|--------|
| [OpenClaw](openclaw/) | npm | `npm install @agentrux/agentrux-openclaw-plugin` | Published |
| [Agent SDK](agent-sdk/) | PyPI | `pip install agentrux-agent-tools` | Beta |
| [Dify](dify/) | Marketplace | Dify Marketplace で "AgenTrux" を検索 | Beta |

> **2026-05-02 retired**: n8n / Flowise / Langflow / Temporal / MCP Server プラグインは公開停止しました。npm の `@agentrux/n8n-plugin` / `flowise-node-agentrux` は deprecate、PyPI の `langflow-agentrux` / `temporal-agentrux` / `agentrux-mcp` は yank 済みです（MCP は API として内蔵されました）。

## 認証フロー（共通）

全プラグインで共通の認証手順:

```
Device Flow (agentrux login) OR OAuth 2.1 Authorization Code (PKCE) → TokenBundle → /oauth/token refresh
JWT (access_token) → Authorization: Bearer で API 呼び出し
```

`POST /oauth/token` は RFC 6749 §6 に準拠した form-encoded `grant_type=refresh_token` でローテーションします。
ヘッドレス用途では `grant_type=client_credentials`（RFC 6749 §4.4）も利用可能です。

## リリース

タグをプッシュすると GitHub Actions が自動で公開します。

```bash
# Python plugin (PyPI)
git tag agent-sdk-v0.1.0b1 && git push origin agent-sdk-v0.1.0b1

# Node.js plugin (npm)
git tag openclaw-v0.14.5 && git push origin openclaw-v0.14.5
```

タグ形式: `<plugin>-v<version>`

## 開発

```bash
# Python plugin
cd agent-sdk
pip install -e .
pytest

# Node.js plugin
cd openclaw
npm install
npm run build
npm test
```

## ライセンス

MIT
