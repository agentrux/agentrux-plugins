# AgenTrux Plugins (Experimental)

AgenTrux を外部プラットフォームから使うためのガイド。

> **Status: Experimental** — API は変更される可能性があります。

## Dify

OpenAPI spec をインポートするだけで使えます。コード不要。

1. Dify → Studio → Tools → Custom Tool → Create
2. Import from URL:
   ```
   https://your-agentrux-server.example.com/script/openapi.json
   ```
3. Auth Type: Bearer Token → JWT を入力
4. Agent に追加

Console / Admin エンドポイントは含まれません。

---

## Python SDK

```bash
pip install git+https://github.com/your-org/AgenTrux.git
```

```python
from agentrux.sdk.facade import AgenTruxClient

client = await AgenTruxClient.bootstrap(
    base_url="https://api.your-server.example.com",
    activation_token="atk_...",
)

await client.publish("topic-uuid", "hello.world", {"msg": "Hello!"})
```

---

## A2A Agent Card

AI エージェント向け。カードを読むだけで AgenTrux を自律的に操作できます。

```
GET /.well-known/agent-card.json
```

---

## 認証フロー（共通）

```
Activation Token → POST /auth/activate → script_id + secret
script_id + secret → POST /auth/token → JWT
JWT → Authorization: Bearer で API 呼び出し
```

## ライセンス

MIT
