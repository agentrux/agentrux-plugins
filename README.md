# AgenTrux Plugins

AgenTrux を外部プラットフォームから使うためのプラグイン集。

> **Status: Beta** — API は変更される可能性があります。

**初めての方:** まず [セットアップガイド](https://app.agentrux.com/docs/setup) で Domo の作成、および Topic・Script・Grant の作成と Activation Code の発行を行ってください。

## プラグイン一覧

| Plugin | Registry | Install | Status |
|--------|----------|---------|--------|
| [n8n](n8n/) | npm | `npm install @agentrux/n8n-plugin` | Published |
| [OpenClaw](openclaw/) | npm | `npm install @agentrux/agentrux-openclaw-plugin` | Published |
| [Flowise](flowise/) | npm | `npm install flowise-node-agentrux` | Beta |
| [Agent SDK](agent-sdk/) | PyPI | `pip install agentrux-agent-tools` | Beta |
| [MCP Server](mcp/) | PyPI | `pip install agentrux-mcp` | Beta |
| [Langflow](langflow/) | PyPI | `pip install langflow-agentrux` | Beta |
| [Temporal](temporal/) | PyPI | `pip install temporal-agentrux` | Beta |
| [Dify](dify/) | Marketplace | Dify Marketplace で "AgenTrux" を検索 | Beta |

## 認証フロー（共通）

全プラグインで共通の認証手順:

```
Activation Code → POST /auth/activate → script_id + client_secret
script_id + client_secret → POST /auth/token → JWT
JWT → Authorization: Bearer で API 呼び出し
```

## リリース

タグをプッシュすると GitHub Actions が自動で公開します。

```bash
# Python plugin (PyPI)
git tag agent-sdk-v0.1.0b1 && git push origin agent-sdk-v0.1.0b1

# Node.js plugin (npm)
git tag flowise-v0.1.0-beta.1 && git push origin flowise-v0.1.0-beta.1
```

タグ形式: `<plugin>-v<version>`

## 開発

```bash
# Python plugin
cd agent-sdk
pip install -e .
pytest

# Node.js plugin
cd flowise
npm install
npm run build
npm test
```

## ライセンス

MIT
