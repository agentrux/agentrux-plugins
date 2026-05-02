# CLAUDE.md - agentrux-plugins リリースルール

## このリポジトリの役割

パブリックリポジトリ。全プラグインと SDK の公開元。
ソースの原本はメインリポジトリ（agentrux/agentrux）の `plugins/` と `src/agentrux/sdk/`。

## リリース手順

### 1. 同期（メインリポジトリ → このリポジトリ）

```bash
# SDK
rsync -av --exclude='__pycache__' \
  /path/to/AgentRux/src/agentrux/sdk/ \
  sdk/src/agentrux/sdk/

# 各プラグイン（例: mcp）
rsync -av --exclude='__pycache__' --exclude='node_modules' \
  --exclude='dist' --exclude='*.egg-info' \
  /path/to/AgentRux/plugins/<name>/ \
  <name>/
```

### 2. バージョン更新

- Python: `pyproject.toml` の `version` を更新
- Node.js: `package.json` の `version` を更新
- SDK を更新した場合、依存するプラグインの `agentrux-sdk>=` も更新

### 3. コミット・タグ・プッシュ

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
| openclaw/ | `@agentrux/agentrux-openclaw-plugin` | `openclaw-v` |

### 廃止プラグイン (2026-05-02)

下記は公開停止。新規バージョンは publish しない。

| パッケージ | レジストリでの状態 |
|---|---|
| `@agentrux/n8n-plugin` | npm: deprecate |
| `flowise-node-agentrux` | npm: deprecate |
| `langflow-agentrux` | PyPI: yank（全 versions） |
| `temporal-agentrux` | PyPI: yank（全 versions） |
| `agentrux-mcp` | PyPI: yank（API として内蔵されたため） |
