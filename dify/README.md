# AgenTrux for Dify

Dify (Cloud / self-hosted ≥ 1.10) から AgenTrux PubSub にアクセスするネイティブ Plugin。

> 日本語 README はこのファイル。English version: [`README.en.md`](./README.en.md)

## パッケージ構成

このリポジトリは 2 種類の Dify Plugin を公開しています。

| Package | 種類 | 用途 | 最新 .difypkg |
|---|---|---|---|
| **Tools** (`agentrux-tools`) | Tool plugin | workflow / agent が Topic に publish / read する | [`dify-agentrux-tools-1.1.0.difypkg`](./dify-agentrux-tools-1.1.0.difypkg) |
| **Trigger** (`agentrux-trigger`) | Trigger plugin | Topic への新着 event で workflow を起動する | [`dify-agentrux-trigger-0.4.0.difypkg`](./dify-agentrux-trigger-0.4.0.difypkg) |

Composer から round-trip (Composer → Dify workflow が編集 → Composer に返却) を組むには 2 つ揃えて install します。 Tools だけでも publish-only workflow は作れます。

## バージョン履歴 (Tools)

| Plugin version | 認証方式 | 状態 |
|---|---|---|
| `v0.x` (~0.3.0) | Activation Code → `/auth/activate` (legacy) | サーバー側 `/auth/activate` 廃止につき **動作しません** |
| `v1.0.0` | OAuth 2.1 (Auth Code + PKCE) または `client_credentials`、 endpoint ハードコード | 動作するが endpoint URL 固定 |
| **`v1.1.0`** | OAuth 2.1 + [RFC 8414](https://datatracker.ietf.org/doc/html/rfc8414) `/.well-known/oauth-authorization-server` 動的 discovery | 推奨 (※ 下記 **既知の問題** 参照) |

`v1.1.0` で metadata discovery を導入したため、 将来 AgenTrux のバックエンド URL が移動しても plugin を再リリースせず透過的に追従できます。

### 既知の問題 (v1.1.0)

`client_credentials` 直接入力モード (下記 §B) で、 plugin が `client_id` の prefix を `script_` 強制チェックします。 一方 AgenTrux サーバーは `crd_<uuid>` 形式の client_id しか発行しないため、 **§B 経路は現状動作しません**。

→ 回避策: **§A の OAuth Authorization Code (PKCE) 経路を使ってください** (こちらは動作確認済)。

## インストール

### Tools plugin

1. Dify の **Studio → Tools → Install Plugin → Local Package**
2. [`dify-agentrux-tools-1.1.0.difypkg`](./dify-agentrux-tools-1.1.0.difypkg) をアップロード
3. プラグイン詳細画面で **Authorize / Connect** をクリック → 後述 §A の OAuth flow へ

### Trigger plugin

1. Dify の **Studio → Triggers → Install Plugin → Local Package**
2. [`dify-agentrux-trigger-0.4.0.difypkg`](./dify-agentrux-trigger-0.4.0.difypkg) をアップロード
3. workflow editor で **Trigger node → AgenTrux: New Event** を追加 → Subscription を作成
4. Subscription 設定で `base_url` (= `https://api.agentrux.com`) と Activation Code (`act_<base64>`、 Console で発行) を入力
5. delivery_mode は **webhook** (Dify が public に到達可能な場合) または **sse** (NAT 内 self-hosted、 plugin が outbound SSE を張る) を選択

## 認証方式 (Tools): 2 通り

Dify の Plugin OAuth schema に対応しているため、 **OAuth Authorization Code (PKCE)** が推奨です。

### §A. OAuth Authorization Code + PKCE (推奨)

Dify Cloud と、 公開到達性のある self-hosted Dify で利用可能。

1. **OAuth Client を `POST /oauth/register` (RFC 7591 DCR) で登録**

   ```bash
   curl -X POST https://api.agentrux.com/oauth/register \
     -H 'Content-Type: application/json' \
     -d '{
       "client_name": "My Dify Plugin",
       "redirect_uris": ["<Dify が表示する callback URL>"],
       "grant_types": ["authorization_code", "refresh_token"],
       "token_endpoint_auth_method": "none"
     }'
   ```

   応答:
   ```json
   {
     "client_id": "dcr_<uuid>",
     "client_id_issued_at": ...,
     "registration_access_token": "rat_<base64>",
     ...
   }
   ```

   注: AgenTrux Console には現在 OAuth Client 一覧 UI は無く、 `POST /oauth/register` API で登録するか、 device flow (= agent-sdk の `agentrux login`) で発行された OAuth Client を流用します。

2. Dify の Plugin 画面で **Authorize** → **OAuth** タブ
3. `base_url` (`https://api.agentrux.com`)、 `client_id` (= `dcr_<uuid>`) を入力。 `client_secret` は public client (PKCE) なら空欄
4. **Connect** → AgenTrux Console の Consent 画面 → **Allow**
5. Dify が JWT を保管し、 期限切れ前に自動で `refresh_token` ローテーション

### §B. `client_credentials` フォールバック ← **v1.1.0 では現状動作しません**

設計上は NAT 内 self-hosted 環境 (Dify が外部からの callback を受けられない場合) 用の fallback ですが、 v1.1.0 plugin は `client_id` の prefix を `script_` 強制チェックする一方、 サーバーは `crd_<uuid>` しか発行しないため、 **このモードは現在使えません**。 §A の OAuth path を使ってください。

直近の修正リリースで prefix チェック緩和を予定しています。

## 提供される Tool (v1.1.0)

| Tool | 動作 | 必要 scope |
|---|---|---|
| `agentrux_publish` | Topic にイベント発行 | `topic.write` |
| `agentrux_read` | Topic からイベント読み取り | `topic.read` |
| `agentrux_upload` | Topic にバイナリ payload アップロード (最大 15 MB) | `topic.write` |

`topic_id` は dynamic-select で、 現在の JWT scope から自動列挙されます。 `agentrux_publish` / `agentrux_upload` は write 権限のある topic のみ、 `agentrux_read` は read 権限のある topic のみ表示。

## 提供される Trigger Event (v0.4.0)

| Event | 動作 | output 変数 |
|---|---|---|
| `new_event` | Topic に新規 event が publish されたとき発火 | `event_id`, `sequence_number`, `event_type`, `topic_id`, `message`, `request_id`, `conversation_key`, `group_id`, `payload_json`, `metadata_json`, `attachment_urls` |

trigger 内の `event_type_filter` parameter で特定 event_type のみ受信可能 (例: `composer.text` のみ拾う)。

## 旧版 (Activation Code 方式) について

- 同梱の `dify-agentrux-tools-0.2.x` / `0.3.x` は **deprecated**
- サーバー側 `/auth/activate` 廃止 (Phase 1.9 で `/auth/redeem-activation-code` に交代) のため動作しません
- v1.0 系から v1.1.0 への移行は **Plugin を一度削除 → v1.1.0 を再 Install → Authorize** の順

## トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `client_id must start with 'script_'` | v1.1.0 client_credentials path が server prefix と不整合 (上記既知の問題) | §A の OAuth path に切替 |
| `unauthorized_client: client_id must be in 'crd_<uuid>' form` | server が `crd_` prefix を要求 | Console で Script Credential を発行し直し |
| `AgenTrux rejected client_credentials (HTTP 401)` | `client_secret` mismatch / 無効化 | Console で再発行 |
| `OAuth state mismatch` | callback URL が plugin プロセス再起動後 | Authorize をやり直す |
| Topic が dynamic-select に出ない | JWT scope に topic 権限がない | Console で Grant を作成 |
| Trigger の workflow が走らない | (1) Subscription 作成のみで workflow に bind してない、 (2) delivery_mode=webhook で plugin endpoint に外部到達できない | (1) workflow editor で Trigger node が published か確認、 (2) self-host なら delivery_mode=sse に切替 |

## 開発者向け

- **Tools ソース**: [`src-1.1.0/`](./src-1.1.0/) — `provider/agentrux_tools.py` (OAuth methods), `provider/agentrux_api.py` (runtime), `tools/{publish,read,upload}.{py,yaml}`
- **Tools 単体テスト**: `tests/` (`pytest -x`)
- **Trigger ソース**: 0.4.0 は .difypkg のみ公開 (ソース 未同梱)。 最新 source 開発版は内部リポジトリで継続中
- minimum Dify: **1.10.0**
- minimum dify_plugin SDK: **0.4.2**

## 関連 ドキュメント

- 認証 contract: [AgenTrux API: OAuth 2.1](https://api.agentrux.com/.well-known/oauth-authorization-server)
- Composer event 共通フォーマット (Tools + Trigger が準拠): [composer_event_format.md](../docs/composer_event_format.md) (公開 doc は別途検討中、 内部 spec)
