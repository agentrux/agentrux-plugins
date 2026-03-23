# AgenTrux Plugins (Experimental)

AgenTrux を外部プラットフォームから使うためのガイド。

> **Status: Experimental** — API は変更される可能性があります。

**初めての方:** まず [セットアップガイド](/docs/setup) で Topic・Script・Grant の作成と Activation Code の発行を行ってください。

## n8n

```bash
npm install @agentrux/n8n-plugin
```

1. n8n → Settings → Community Nodes → Install → `@agentrux/n8n-plugin`
2. Credential 作成 → "Activation Token" モードで token を入力
3. AgenTrux ノードで publish / read
4. AgenTrux Trigger で polling / webhook 受信

---

## OpenClaw

```bash
npm install @agentrux/openclaw-plugin
```

```
OpenClaw> AgenTrux に接続して。setup code は setup_...
OpenClaw> sensor-data に温度 22.5 度を送って
OpenClaw> Bob に「会議資料まとめて」と送って
```

---

## Dify

OpenAPI spec をインポートするだけ。コード不要。

1. Dify → Studio → Tools → Custom Tool → Create
2. Import from URL:
   ```
   {{API_URL}}/script/openapi.json
   ```
3. Auth Type: Bearer Token → JWT を入力
4. Agent に追加

Console / Admin エンドポイントは含まれません。

---

## Python SDK

```bash
pip install git+https://github.com/agentrux/agentrux.git
```

```python
from agentrux.sdk.facade import AgenTruxClient

client = await AgenTruxClient.bootstrap(
    base_url="{{API_URL}}",
    activation_code="ac_...",
)

await client.publish("topic-uuid", "hello.world", {"msg": "Hello!"})
```

---

## A2A Agent Card

AI エージェント向け。カードを読むだけで AgenTrux を自律的に操作できます。

```
{{API_URL}}/.well-known/agent-card.json
```

---

## 認証フロー（共通）

```
Activation Code → POST /auth/activate → script_id + client_secret
script_id + client_secret → POST /auth/token → JWT
JWT → Authorization: Bearer で API 呼び出し
```

## ライセンス

MIT
