"""CheckpointStore - Persists last processed sequence_no per topic.

See doc/詳細設計/05_ビルディングブロック/11_PullPubSub_SDK詳細設計.md §11.

Design summary:
- JSONL append-only file (one record per save)
- fsync=True by default (durability over throughput)
- Separate <path>.lock file for fcntl.flock (independent lifecycle from
  the data file, so reset()/recreate doesn't break the lock)
- Strict in-order: save() raises CheckpointOrderError on backward seq
- Save timing: caller invokes save() AFTER the user callback completes
  successfully — this is the at-least-once guarantee
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from agentrux.sdk.errors import CheckpointLockedError, CheckpointOrderError

logger = logging.getLogger("agentrux.sdk.checkpoint")


@dataclass
class CheckpointStats:
    saves_total: int = 0
    loads_total: int = 0
    parse_errors: int = 0
    file_size_bytes: int = 0
    last_save_seq: dict[str, int] = field(default_factory=dict)


class CheckpointStore(ABC):
    """Persists (sequence_no, event_id) per topic across process restarts."""

    @abstractmethod
    async def save(self, topic_id: str, sequence_no: int, event_id: str) -> None:
        """Record the latest processed event. Call AFTER callback success.

        Raises CheckpointOrderError if sequence_no is older than the last
        successful save for the same topic_id.
        """

    @abstractmethod
    async def load(self, topic_id: str) -> tuple[int, str] | None:
        """Return (sequence_no, event_id) for the topic, or None."""

    @abstractmethod
    async def reset(self, topic_id: str) -> None:
        """Discard the checkpoint for one topic."""

    @abstractmethod
    async def close(self) -> None:
        """Release file locks and other resources."""


class FileCheckpointStore(CheckpointStore):
    """JSONL append-only file-based checkpoint store.

    File format (one record per line):
        {"topic_id": "...", "sequence_no": 42, "event_id": "...", "ts": 1.23}

    On startup, the file is scanned and the latest record per topic_id is
    cached in memory.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        fsync: bool = True,
    ):
        self._path = Path(path)
        self._lock_path = Path(str(self._path) + ".lock")
        self._fsync = fsync
        self._cache: dict[str, tuple[int, str]] = {}
        self._lock_fd: int | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._saves_total = 0
        self._loads_total = 0
        self._parse_errors = 0

        self._acquire_process_lock()
        self._load_from_disk()

    # --- Process-level lock (separate lockfile) ---

    def _acquire_process_lock(self) -> None:
        # Ensure parent dir exists
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as e:
            raise CheckpointLockedError(
                f"Cannot open lockfile {self._lock_path}: {e}"
            ) from e
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            os.close(fd)
            raise CheckpointLockedError(
                f"Checkpoint lock held by another process: {self._lock_path}"
            ) from e
        # Best-effort: write current pid for debugging
        try:
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        except OSError:
            pass
        self._lock_fd = fd

    def _release_process_lock(self) -> None:
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._lock_fd)
        except OSError:
            pass
        self._lock_fd = None

    # --- Persistence ---

    def _load_from_disk(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        self._parse_errors += 1
                        logger.warning(
                            "checkpoint: skipping malformed line %d in %s",
                            lineno, self._path,
                        )
                        continue
                    topic_id = rec.get("topic_id")
                    seq = rec.get("sequence_no")
                    eid = rec.get("event_id")
                    tombstone = rec.get("tombstone", False)
                    if not isinstance(topic_id, str):
                        self._parse_errors += 1
                        logger.warning(
                            "checkpoint: skipping invalid record at line %d", lineno,
                        )
                        continue
                    if tombstone:
                        # reset() marker — forget any prior state
                        self._cache.pop(topic_id, None)
                        continue
                    if not isinstance(seq, int) or not isinstance(eid, str):
                        self._parse_errors += 1
                        logger.warning(
                            "checkpoint: skipping invalid record at line %d", lineno,
                        )
                        continue
                    # Append-only with monotonic seq → last record wins
                    self._cache[topic_id] = (seq, eid)
        except OSError as e:
            logger.warning("checkpoint: cannot read %s: %s", self._path, e)

    def _append_record_sync(self, record: dict) -> None:
        """Synchronous append + fsync (run in a thread)."""
        line = json.dumps(record, separators=(",", ":")) + "\n"
        # Ensure parent dir exists
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode; fsync after write if requested
        fd = os.open(
            str(self._path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, line.encode())
            if self._fsync:
                os.fsync(fd)
        finally:
            os.close(fd)

    # --- Public API ---

    async def save(self, topic_id: str, sequence_no: int, event_id: str) -> None:
        if self._closed:
            raise RuntimeError("CheckpointStore is closed")
        async with self._write_lock:
            cur = self._cache.get(topic_id)
            if cur is not None and sequence_no < cur[0]:
                raise CheckpointOrderError(
                    f"save() backward: topic={topic_id} "
                    f"current={cur[0]} requested={sequence_no}"
                )
            record = {
                "topic_id": topic_id,
                "sequence_no": sequence_no,
                "event_id": event_id,
                "ts": time.time(),
            }
            await asyncio.to_thread(self._append_record_sync, record)
            self._cache[topic_id] = (sequence_no, event_id)
            self._saves_total += 1

    async def load(self, topic_id: str) -> tuple[int, str] | None:
        if self._closed:
            raise RuntimeError("CheckpointStore is closed")
        self._loads_total += 1
        return self._cache.get(topic_id)

    async def reset(self, topic_id: str) -> None:
        if self._closed:
            raise RuntimeError("CheckpointStore is closed")
        async with self._write_lock:
            if topic_id in self._cache:
                del self._cache[topic_id]
            # Append a tombstone so the on-disk view also forgets it
            record = {
                "topic_id": topic_id,
                "sequence_no": -1,
                "event_id": "",
                "ts": time.time(),
                "tombstone": True,
            }
            await asyncio.to_thread(self._append_record_sync, record)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._release_process_lock()

    @property
    def stats(self) -> CheckpointStats:
        try:
            size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            size = 0
        return CheckpointStats(
            saves_total=self._saves_total,
            loads_total=self._loads_total,
            parse_errors=self._parse_errors,
            file_size_bytes=size,
            last_save_seq={k: v[0] for k, v in self._cache.items()},
        )
