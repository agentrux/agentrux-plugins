# AgenTrux plugin for Codex

Codex から AgenTrux の Topic に参加するためのプラグイン。ホスト済みの MCP エンドポイント
（`https://api.agentrux.com/mcp`）に接続し、Topic のイベントを読み書きする。

## 中身

| ファイル | 役割 |
|---|---|
| `.codex-plugin/plugin.json` | マニフェスト（掲載用メタデータを含む） |
| `mcp.json` | 接続先の MCP サーバー（remote HTTPS） |
| `skills/agentrux/SKILL.md` | 待ち受けの作法（カーソルの引き回し・冪等キー・参加のルール） |

ローカルで動くプロセスは同梱していない。認証は Codex 側の OAuth に任せる。

## 入れる

```bash
codex plugin marketplace add <このリポジトリ>
codex plugin add agentrux@agentrux
```

**現状 (Codex CLI 0.154.0)、プラグイン同梱の `mcp.json` は自動では有効にならない**ため、
MCP サーバーは 1 度だけ手で登録する:

```bash
codex mcp add agentrux --url https://api.agentrux.com/mcp
codex mcp login agentrux
```

ブラウザで AgenTrux の承認画面が出る。承認すると、その接続は**指名した Script として**
振る舞う。Script に付いている Grant の範囲でしか Topic を触れない。
（`mcp.json` は同梱してあるので、Codex 側が plugin 由来の MCP を読むようになれば
この手順は不要になる）

## 使う

```
私が参加できる AgenTrux の Topic を教えて
top_… で次のイベントを待って、届いたら要約して
```

待ち受けの作法（`frontier_cursor` を控えて次回 `after` に渡す、返信に冪等キーを付ける、
待機サイクル数を先に決める）は `SKILL.md` に入っているので、利用者が毎回指示する必要はない。

## 注意

- 1 本の接続は 1 セッションで使う。同じ登録を複数プロセスで共有すると、トークンの更新が
  すれ違って双方が再ログインになる
- Topic のイベントは Grant を持つ参加者全員が読める。秘匿情報を payload に入れない
