# CLAUDE.md - agentrux-plugins リリースルール

## このリポジトリの役割

パブリックリポジトリ。全プラグインと SDK の**公開元かつ唯一の原本（SSOT）**。

SDK のコードはこのリポジトリ（`sdk/src/agentrux_sdk/`）にしか存在しない。
メインリポジトリ（agentrux/agentrux）からの **rsync / コピー同期は廃止**（2026-05-29）。
コピーは SSOT を崩すため禁止。SDK の修正は直接このリポジトリで行う。

- 公開 import 名は `agentrux_sdk`（PyPI 名 `agentrux-sdk`）。
- server 内部の `agentrux.sdk` とは別 namespace。両者は衝突しない。
- メインリポジトリの server 本体は SDK に依存しないため、main 側に SDK のコピーは置かない。

## リリース手順

### 1. バージョン更新

- Python: `pyproject.toml` の `version` を更新
- Node.js: `package.json` の `version` を更新
- SDK を更新した場合、依存するプラグインの `agentrux-sdk>=` も更新

### 2. コミット・タグ・プッシュ

```bash
git add -A && git commit -m "release: <plugin> v<version>"
git push origin main
git tag <plugin>-v<version>
git push origin <plugin>-v<version>
```

タグ形式: `<plugin>-v<version>`（例: `sdk-v0.1.0b1`, `mcp-v0.1.0b2`, `openclaw-v0.14.5`）

GitHub Actions がタグを検知して自動で PyPI / npm に公開する。

## 公開前チェックリスト（必須）

- [ ] サーバー本体のコード（api/, auth/, infrastructure/, models/, config.py）が含まれていないこと
- [ ] テストコード（tests/, __tests__/, *test*）がパッケージに含まれないこと（.npmignore / hatch 設定で除外）
- [ ] ハードコードされた秘匿情報がないこと
- [ ] 非公開 API エンドポイント（/admin/*, /console/*）への参照がないこと

## 絶対禁止

- **メインリポジトリから直接 PyPI / npm に publish しない**（サーバー本体が公開される）
- **`agentrux` というパッケージ名で PyPI に publish しない**（SDK は `agentrux-sdk`）
- **秘匿情報（トークン、パスワード、秘密鍵）をコミットしない**
- **GitHub Secrets（PYPI_API_TOKEN, NPM_TOKEN）をログや出力に表示しない**

## パッケージ構成

| ディレクトリ | PyPI / npm パッケージ名 | タグ prefix |
|------------|----------------------|------------|
| sdk/ | `agentrux-sdk` | `sdk-v` |
| agent-sdk/ | `agentrux-agent-tools` | `agent-sdk-v` |
| openclaw/ | `@agentrux/openclaw-plugin` | `openclaw-v` |
| n8n/ | `@agentrux/n8n-nodes-agentrux` | `n8n-v` |

### 廃止プラグイン (2026-05-02)

下記は公開停止。新規バージョンは publish しない。

| パッケージ | レジストリでの状態 |
|---|---|
| `@agentrux/n8n-plugin` | npm: deprecate（旧名。後継は新名 `@agentrux/n8n-nodes-agentrux` = 上表、2026-06-01 復活） |
| `flowise-node-agentrux` | npm: deprecate |
| `langflow-agentrux` | PyPI: yank（全 versions） |
| `temporal-agentrux` | PyPI: yank（全 versions） |
| `agentrux-mcp` | PyPI: yank（API として内蔵されたため） |
