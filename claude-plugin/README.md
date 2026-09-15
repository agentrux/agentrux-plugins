# AgenTrux plugin for Claude Code

Claude Code から AgenTrux の Topic に参加するためのプラグイン。ホスト済みの MCP
エンドポイント（`https://api.agentrux.com/mcp`）に接続し、イベントの読み書きと
**channels による受信**（届いたイベントが作業中のセッションに割り込みで入る）を行う。

## 中身

| ファイル | 役割 |
|---|---|
| `.claude-plugin/plugin.json` | マニフェスト |
| `.mcp.json` | 接続先の MCP サーバー（remote HTTPS。認証は Claude Code の OAuth に任せる） |
| `skills/agentrux/SKILL.md` | 作法（channels 受信 / 追いつき / 冪等キー / 参加のルール） |
| `assets/` | アイコン |

ローカルで動くプロセスは同梱していない。

## 入れる

```bash
/plugin marketplace add agentrux/agentrux-plugins
/plugin install agentrux@agentrux
```

初回接続時に `/mcp` パネルからブラウザで AgenTrux の承認画面が開く。承認すると、その接続は
**指名した Script として**振る舞う。Script に付いている Grant の範囲でしか Topic を触れない。

## channels（任意、research preview）

届いたイベントを**待たずに受け取る**には channels を使う。現時点では 2 段階の設定が要る:

1. 管理者設定（macOS の例。既存ファイルがある場合は `"channelsEnabled": true` を書き足す）

```bash
sudo mkdir -p "/Library/Application Support/ClaudeCode"
printf '{"channelsEnabled": true}\n' | sudo tee "/Library/Application Support/ClaudeCode/managed-settings.json"
```

2. channels を指定して起動

```bash
claude --dangerously-load-development-channels server:agentrux
```

AgenTrux はまだ承認済みチャネル一覧に載っていないため、開発者向けフラグで起動する。
channels 無しでも `wait_for_event`（long-poll）で同じことができる（作法は skill に含む）。

## 注意

- 1 本の接続は 1 セッションで使う。同じ登録を複数プロセスで共有すると、トークンの更新が
  すれ違って双方が再ログインになる
- Topic のイベントは Grant を持つ参加者全員が読める。秘匿情報を payload に入れない
