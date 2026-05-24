# AgenTrux for Dify

Dify (Cloud / self-hosted ≥ 1.10) から AgenTrux PubSub にアクセスするネイティブ Plugin。

> 日本語 README はこのファイル。English version: [`README.en.md`](./README.en.md)

## v1.1.0 の変更点

`v1.1.0` 以降は **`/.well-known/oauth-authorization-server` (RFC 8414)** で
`authorization_endpoint` / `token_endpoint` を動的に discover するようになりました。
URL を plugin 側でハードコードしていないので、将来 AgenTrux のバックエンド URL が
移動しても plugin の再リリースなしで透過的に追従します。

## v1.0.0 の変更点

`v0.x` の **Activation Code (`ac_...`) 方式は廃止** し、**OAuth 2.1** ベースに切り替えました。

| Plugin version | 認証方式 | サーバー |
|----------------|----------|----------|
| `v0.x`         | Activation Code → `/auth/activate` (legacy)     | 旧認証層（停止済み） |
| `v1.0.0`       | OAuth 2.1 (Auth Code + PKCE) もしくは `client_credentials`、endpoint ハードコード | 新認証層（本番） |
| **`v1.1.0`**   | OAuth 2.1 + RFC 8414 metadata discovery | 新認証層（本番） |

旧版 `v0.2.x` / `v0.3.x` の `.difypkg` は backward-compat のため同梱していますが、新認証層では動作しません。**新規導入は `v1.1.0` をお使いください**。

## インストール

1. Dify の **Studio → Tools → Install Plugin → Local Package**
2. `dify-agentrux-tools-1.1.0.difypkg` をアップロード
3. プラグイン詳細画面で **Authorize / Connect** をクリック

## 認証方式: 2 通り

Dify の Plugin OAuth schema に対応しているため、**OAuth Authorization Code (PKCE)** が
推奨フローです。Dify 側のコールバック URL を AgenTrux 側で受信できない環境
（NAT 内 self-hosted 等）では `client_credentials` のフォールバックも選べます。

### A. OAuth Authorization Code + PKCE（推奨、Dify Cloud / 公開到達性のある self-hosted）

1. **OAuth Client を発行**
   - AgenTrux Console → Settings → OAuth Clients → **Register**
   - もしくは API:
     ```bash
     curl -X POST https://api.agentrux.com/oauth/register \
       -H 'Content-Type: application/json' \
       -d '{
         "client_name": "Dify Plugin",
         "redirect_uris": ["<Dify が表示する callback URL>"],
         "grant_types": ["authorization_code", "refresh_token"]
       }'
     ```
   - `client_id` (`oauth-client_<uuid>`) と (任意で) `client_secret` を控える
2. Dify の Plugin 画面で **Authorize** → **OAuth** タブ
3. `base_url` (`https://api.agentrux.com`)、`client_id`、`client_secret` (Public client は空) を入力
4. **Connect** → AgenTrux Console の Consent 画面 → **Allow**
5. Dify が JWT を保管し、有効期限切れ前に自動で `refresh_token` ローテーション

### B. `client_credentials` フォールバック（NAT 内 self-hosted, OAuth コールバックを開けない場合）

1. AgenTrux Console → Scripts → **Create Credential**
2. `client_id` (`script_<uuid>`) と `client_secret` を控える
3. Dify の Plugin 画面 → **Authorize** → **API Key** タブ
4. `base_url`, `client_id`, `client_secret` を貼り付け → **Save**

> `client_id` は `script_` プレフィックス必須。素の UUID は AgenTrux 側で拒否されます。

## 提供される Tool

| Tool | 動作 | 必要 scope |
|------|------|-----------|
| `agentrux_publish` | Topic にイベント発行 | `topic.write` |
| `agentrux_read`    | Topic からイベント読み取り | `topic.read` |
| `agentrux_upload`  | Topic にバイナリ payload アップロード（最大 15 MB） | `topic.write` |

`topic_id` は **dynamic-select** で、現在の JWT scope から自動列挙されます。
`agentrux_publish` / `agentrux_upload` は `write` 権限のある topic のみ、`agentrux_read` は `read` 権限のある topic のみ表示。

## 旧版（Activation Code 方式）について

- 同梱の `dify-agentrux-tools-0.3.0.difypkg` 以下は **deprecated**
- 新認証層 (`/auth/activate` 廃止) では動作しません
- 既存環境を `v1.0.0` に移行する場合は、**Plugin を一度削除 → v1.0.0 を再 Install → Authorize** の順で実施

## トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| `client_id must start with 'script_'` | Console UUID をそのまま貼り付けた | Console で発行した `script_` 付き ID を使う |
| `AgenTrux rejected client_credentials (HTTP 401)` | `client_secret` mismatch / 無効化 | Console で再発行 |
| `OAuth state mismatch` | コールバック URL が plugin プロセス再起動後 | Authorize をやり直す |
| Topic が dynamic-select に出ない | JWT scope に topic 権限がない | Console で Grant を作成 |

## 開発者向け

- ソース: `provider/agentrux_tools.py` (OAuth methods), `provider/agentrux_api.py` (runtime)
- 単体テスト: `tests/` (`pytest -x`)
- minimum Dify: **1.10.0**
- minimum dify_plugin SDK: **0.4.2**
