# AgenTrux for Dify

Dify Cloud / Self-hosted に AgenTrux を統合する最も簡単な方法。

## 方法1: OpenAPI インポート（推奨、コード不要）

AgenTrux は Script API 用の OpenAPI spec を自動生成します。

### 手順

1. **Dify にログイン** → Studio → Tools → Custom Tool → Create

2. **OpenAPI Schema をインポート**
   ```
   URL: https://your-agentrux-server.example.com/script/openapi.json
   ```
   「Import from URL」でスキーマを取得。

3. **認証設定**
   - Auth Type: **Bearer Token**
   - Token: AgenTrux の JWT（`POST /auth/token` で取得）

4. **エージェントに追加**
   - Studio → Agent → Tools → 「AgenTrux Script API」を追加

5. **使う**
   ```
   チャット: 「sensor-data トピックに温度 22.5 度を送って」
   → エージェントが POST /topics/{id}/events を呼ぶ
   ```

### 含まれるエンドポイント

| Method | Path | Description |
|--------|------|-------------|
| POST | /auth/activate | Activation token → credentials |
| POST | /auth/token | Get JWT |
| POST | /auth/refresh | Refresh JWT |
| POST | /auth/redeem-grant | Redeem grant token |
| GET | /auth/me | Current user info |
| POST | /topics/{id}/events | Publish event |
| GET | /topics/{id}/events | List events |
| GET | /topics/{id}/events/stream | SSE stream |
| GET | /topics/{id}/events/{event_id} | Get single event |
| POST | /topics/{id}/payloads | Upload payload |
| GET | /topics/{id}/payloads/{object_id} | Download payload |

**Console / Admin エンドポイントは含まれません。**

## 方法2: Dify Plugin SDK（高度な制御）

より細かい制御が必要な場合、Dify Plugin SDK で AgenTrux プラグインを作成できます。

```python
# dify_plugin/agentrux_provider.py
from dify_plugin import ToolProvider
from dify_plugin.entities.tool import ToolInvokeMessage

class AgenTruxProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict) -> None:
        # POST /auth/token で検証
        ...

class PublishEventTool(Tool):
    def _invoke(self, tool_parameters: dict) -> ToolInvokeMessage:
        # POST /topics/{id}/events
        ...
```

## JWT の自動更新

Dify の Custom Tool は静的な Bearer Token を使うため、JWT の自動更新はできません。

対策:
- **方法A**: Dify Workflow で最初に `/auth/token` を呼び、結果の JWT を後続ステップで使う
- **方法B**: AgenTrux の JWT TTL を長く設定（e.g. 24h）
- **方法C**: Dify Plugin SDK で自動更新ロジックを実装
