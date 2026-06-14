"""Checkpoint store for pipeline (in-memory + file-based).

SSOT: docs/04_design/sdk/sdk_design.md §6

checkpoint は処理成功 event の **opaque cursor** (cluster_agnostic_ordering.md §3-3) を
保存する。 event_id ではなく cursor を保存することで:
  - event 行が retention 落ちしても idle 後の偽 RETENTION_MISS を避ける
  - cursor には created_at が内包されているので、 true RETENTION_MISS のみを検出できる
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path


class CheckpointStore(ABC):
    """pipeline 処理成功 event のみ commit するための store.

    保存・ロードする値は opaque cursor 文字列 (event_id ではない)。
    cursor は Event.cursor フィールドから取得する。
    """

    @abstractmethod
    async def load(self, topic_id: str) -> str | None:
        """最後に commit した opaque cursor を返す (なければ None)."""

    @abstractmethod
    async def commit(self, topic_id: str, cursor: str) -> None:
        """opaque cursor を最新 checkpoint として永続化."""


class InMemoryCheckpointStore(CheckpointStore):
    """test / 短命 process 用."""

    def __init__(self) -> None:
        self._cp: dict[str, str] = {}

    async def load(self, topic_id: str) -> str | None:
        return self._cp.get(topic_id)

    async def commit(self, topic_id: str, cursor: str) -> None:
        self._cp[topic_id] = cursor


class FileCheckpointStore(CheckpointStore):
    """JSON ファイル 1 つに { topic_id: cursor } を保存。 1 process 専用 (file lock なし).

    本番では FileCheckpointStore より DB ベースの実装を推奨 (本 SDK では skeleton のみ).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._cache: dict[str, str] = {}
        if self._path.exists():
            try:
                self._cache = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._cache = {}

    async def load(self, topic_id: str) -> str | None:
        return self._cache.get(topic_id)

    async def commit(self, topic_id: str, cursor: str) -> None:
        self._cache[topic_id] = cursor
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
