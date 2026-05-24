"""Tests for FileCheckpointStore.

Covers:
- save/load/reset round-trip
- ordering violation raises CheckpointOrderError
- legacy `sequence_no` records are skipped
- concurrent process holding the lock raises CheckpointLockedError
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentrux.sdk.checkpoint import FileCheckpointStore
from agentrux.sdk.errors import CheckpointLockedError, CheckpointOrderError


pytestmark = pytest.mark.asyncio


@pytest.fixture
def tmp_ck(tmp_path: Path) -> Path:
    return tmp_path / "ck.jsonl"


async def test_save_then_load(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_abc", 42, "evt_x")
        result = await store.load("top_abc")
        assert result == (42, "evt_x")
    finally:
        await store.close()


async def test_save_advances_monotonically(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_a", 1, "evt_1")
        await store.save("top_a", 2, "evt_2")
        await store.save("top_a", 100, "evt_100")
        assert await store.load("top_a") == (100, "evt_100")
    finally:
        await store.close()


async def test_save_backward_raises(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_a", 10, "evt_10")
        with pytest.raises(CheckpointOrderError):
            await store.save("top_a", 5, "evt_5")
    finally:
        await store.close()


async def test_load_unknown_topic_returns_none(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        assert await store.load("top_unknown") is None
    finally:
        await store.close()


async def test_reset_clears_topic(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_a", 10, "evt_10")
        await store.reset("top_a")
        assert await store.load("top_a") is None
    finally:
        await store.close()


async def test_load_skips_legacy_sequence_no_records(tmp_ck: Path) -> None:
    """v0.2 records used `sequence_no`; v0.3 must skip them with a warning."""
    # Pre-populate the file with a legacy record + a current-format record.
    with open(tmp_ck, "w") as f:
        f.write(json.dumps({"topic_id": "top_a", "sequence_no": 5, "event_id": "old", "ts": 0}) + "\n")
        f.write(json.dumps({"topic_id": "top_a", "sequence_number": 10, "event_id": "evt_x", "ts": 0}) + "\n")

    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        assert await store.load("top_a") == (10, "evt_x")
        # Parse_errors counter should reflect the legacy skip.
        assert store.stats.parse_errors >= 1
    finally:
        await store.close()


async def test_malformed_lines_skipped(tmp_ck: Path) -> None:
    with open(tmp_ck, "w") as f:
        f.write("not json\n")
        f.write(json.dumps({"topic_id": "top_a", "sequence_number": 1, "event_id": "evt", "ts": 0}) + "\n")
        f.write(json.dumps({"sequence_number": 99}) + "\n")  # no topic_id

    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        assert await store.load("top_a") == (1, "evt")
        assert store.stats.parse_errors >= 2
    finally:
        await store.close()


async def test_double_open_raises_locked(tmp_ck: Path) -> None:
    store1 = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        with pytest.raises(CheckpointLockedError):
            FileCheckpointStore(tmp_ck, fsync=False)
    finally:
        await store1.close()


async def test_close_then_use_raises(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    await store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await store.save("top_a", 1, "evt_x")
    with pytest.raises(RuntimeError, match="closed"):
        await store.load("top_a")


async def test_close_is_idempotent(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    await store.close()
    await store.close()  # no exception


async def test_save_persists_to_disk(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_a", 42, "evt_x")
    finally:
        await store.close()
    # Re-open the file and check the on-disk record.
    with open(tmp_ck) as f:
        records = [json.loads(line) for line in f if line.strip()]
    assert any(
        r.get("topic_id") == "top_a"
        and r.get("sequence_number") == 42
        and r.get("event_id") == "evt_x"
        for r in records
    )


async def test_stats_reflect_activity(tmp_ck: Path) -> None:
    store = FileCheckpointStore(tmp_ck, fsync=False)
    try:
        await store.save("top_a", 1, "evt_1")
        await store.save("top_a", 2, "evt_2")
        await store.load("top_a")
        s = store.stats
        assert s.saves_total == 2
        assert s.loads_total >= 1
        assert s.last_save_seq.get("top_a") == 2
    finally:
        await store.close()
