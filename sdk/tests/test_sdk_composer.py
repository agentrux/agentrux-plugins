"""SDK composer reader (`iter_composer_groups`) の unit test (Phase BT.1.d 部分実装).

SSOT: docs/04_design/messaging/composer_event_format.md §3-3 receiver responsibilities.

検証観点 (CLAUDE.md §テスト 4 層):
  - a 正常: text + upload N 件 same group → 1 ComposerGroup
  - a 正常: standalone text (group_id 無し) → 1 ComposerGroup (text_event のみ)
  - c 境界: upload のみで flush_timeout 経過 → text_event=None で yield
  - c 境界: upload を蓄積中に stream 終端 → upload-only group で flush
  - c 境界: 異 group_id 並行 (interleaved) → 各 group が独立に yield
  - c 境界: composer 以外の event_type が group_id 付きで来た → standalone 扱い
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from agentrux.sdk.composer import ComposerGroup, iter_composer_groups
from agentrux.sdk.models import Event


def _evt(
    *,
    seq: int,
    event_type: str,
    group_id: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    payload_object_id: str | None = None,
) -> Event:
    md: dict[str, Any] = dict(extra_metadata or {})
    if group_id is not None:
        md["group_id"] = group_id
    return Event(
        event_id=f"evt_{seq:08d}",
        topic_id="top_test",
        event_type=event_type,
        sequence_number=seq,
        stored_at=datetime.now(UTC),
        payload=payload,
        payload_object_id=payload_object_id,
        metadata=md or None,
    )


async def _yield_all(events: list[Event]) -> AsyncIterator[Event]:
    """list[Event] を AsyncIterator にする helper (await 無しの即時 yield)."""
    for e in events:
        yield e


async def _collect(it: AsyncIterator[ComposerGroup]) -> list[ComposerGroup]:
    return [g async for g in it]


# ============================================================================
# a. 正常: text + upload N 件 same group → 1 ComposerGroup として yield
# ============================================================================


@pytest.mark.asyncio
async def test_text_and_uploads_same_group_yields_single_composer_group() -> None:
    gid = "11111111-1111-1111-1111-111111111111"
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=gid, payload_object_id="pob_a"),
        _evt(seq=2, event_type="composer.upload", group_id=gid, payload_object_id="pob_b"),
        _evt(seq=3, event_type="composer.text", group_id=gid, payload={"content": "hello"}),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    g = groups[0]
    assert g.group_id == gid
    assert g.text_event is not None
    assert g.text_event.payload == {"content": "hello"}
    assert len(g.upload_events) == 2
    assert g.upload_events[0].payload_object_id == "pob_a"
    assert g.upload_events[1].payload_object_id == "pob_b"


@pytest.mark.asyncio
async def test_composer_json_kind_is_also_text() -> None:
    """composer.json は text と同じ扱い (group の確定 trigger になる)."""
    gid = "22222222-2222-2222-2222-222222222222"
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=gid),
        _evt(seq=2, event_type="composer.json", group_id=gid, payload={"k": "v"}),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    assert groups[0].text_event is not None
    assert groups[0].text_event.event_type == "composer.json"


# ============================================================================
# a. 正常: standalone text (group_id 無し) → 1 ComposerGroup (text_event のみ)
# ============================================================================


@pytest.mark.asyncio
async def test_standalone_text_without_group_id_yields_immediately() -> None:
    events = [
        _evt(seq=1, event_type="composer.text", group_id=None, payload={"content": "hi"}),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    assert groups[0].group_id is None
    assert groups[0].text_event is not None
    assert groups[0].upload_events == ()


@pytest.mark.asyncio
async def test_standalone_upload_without_group_id_yields_immediately() -> None:
    """添付単独 (group_id 無し) → standalone bubble、 即時 yield."""
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=None, payload_object_id="pob_x"),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    assert groups[0].group_id is None
    assert groups[0].text_event is None
    assert len(groups[0].upload_events) == 1


# ============================================================================
# c. 境界: stream 終端で buffer に残っている group は upload-only で flush
# ============================================================================


@pytest.mark.asyncio
async def test_upload_only_buffer_flushes_on_stream_end() -> None:
    """text が来ないまま stream が終了 → upload-only group として yield."""
    gid = "33333333-3333-3333-3333-333333333333"
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=gid, payload_object_id="pob_a"),
        _evt(seq=2, event_type="composer.upload", group_id=gid, payload_object_id="pob_b"),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    assert groups[0].group_id == gid
    assert groups[0].text_event is None
    assert len(groups[0].upload_events) == 2


# ============================================================================
# c. 境界: flush_timeout 経過 → upload-only group を flush して yield
# ============================================================================


@pytest.mark.asyncio
async def test_flush_timeout_drains_upload_only_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """flush_timeout 経過 後、 次 event 受領契機で buffer の expired group を drain."""
    gid = "44444444-4444-4444-4444-444444444444"
    # 仮想時刻を進めるため time.monotonic を mock
    current = {"t": 1000.0}

    def fake_monotonic() -> float:
        return current["t"]

    monkeypatch.setattr("agentrux.sdk.composer.time.monotonic", fake_monotonic)

    async def event_source() -> AsyncIterator[Event]:
        # 1. upload を 1 件 buffer に入れる
        yield _evt(seq=1, event_type="composer.upload", group_id=gid)
        # 2. 時刻を進める (flush_timeout=2.0 を超過)
        current["t"] = 1010.0
        # 3. 別 group_id の何か (composer 以外) → standalone yield と同時に flush 判定が走る
        yield _evt(seq=2, event_type="user.event", group_id=None, payload={"k": "v"})

    groups = await _collect(iter_composer_groups(event_source(), flush_timeout_seconds=2.0))
    # 期待: [flushed group (upload-only, gid), standalone (user.event)]
    assert len(groups) == 2
    flushed = groups[0]
    assert flushed.group_id == gid
    assert flushed.text_event is None
    assert len(flushed.upload_events) == 1
    # standalone
    assert groups[1].group_id is None


@pytest.mark.asyncio
async def test_flush_timeout_zero_disables_flush() -> None:
    """flush_timeout=0 → 時刻 flush は行われない、 stream 終端でのみ drain."""
    gid = "55555555-5555-5555-5555-555555555555"
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=gid),
        _evt(seq=2, event_type="user.event", group_id=None, payload={"k": "v"}),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events), flush_timeout_seconds=0.0))
    # 期待: [standalone (user.event), stream 終端で flushed (upload-only, gid)]
    assert len(groups) == 2
    assert groups[0].group_id is None  # standalone first (event 順)
    # stream 終端 flush は逆順 (buffer dict iteration)、 group_id=gid のもののみ
    assert groups[1].group_id == gid
    assert groups[1].text_event is None
    assert len(groups[1].upload_events) == 1


# ============================================================================
# c. 境界: 異 group_id 並行 (interleaved) → 各 group が独立に yield
# ============================================================================


@pytest.mark.asyncio
async def test_interleaved_groups_yield_independently() -> None:
    """2 つの group が時間的に重なって流れてきても、 各々 text 着信時に独立に yield."""
    gid_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    gid_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    events = [
        _evt(seq=1, event_type="composer.upload", group_id=gid_a, payload_object_id="pob_a1"),
        _evt(seq=2, event_type="composer.upload", group_id=gid_b, payload_object_id="pob_b1"),
        _evt(seq=3, event_type="composer.text", group_id=gid_b, payload={"content": "b"}),
        _evt(seq=4, event_type="composer.text", group_id=gid_a, payload={"content": "a"}),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 2
    # group_b の text が先に来たので group_b が先に yield される
    assert groups[0].group_id == gid_b
    assert groups[0].text_event.payload == {"content": "b"}
    assert len(groups[0].upload_events) == 1
    assert groups[1].group_id == gid_a
    assert groups[1].text_event.payload == {"content": "a"}
    assert len(groups[1].upload_events) == 1


# ============================================================================
# c. 境界: composer 以外 event_type + group_id 付き → standalone 扱い
# ============================================================================


@pytest.mark.asyncio
async def test_non_composer_event_with_group_id_is_standalone() -> None:
    """spec は composer.text/json/upload に限定。 group_id 付きでも composer 以外は
    standalone group として yield (composer-only filter 経路 + 後方互換)。"""
    events = [
        _evt(
            seq=1,
            event_type="user.event",
            group_id="99999999-9999-9999-9999-999999999999",
            payload={"k": "v"},
        ),
    ]
    groups = await _collect(iter_composer_groups(_yield_all(events)))
    assert len(groups) == 1
    # composer 以外なので text も upload も None / 空
    assert groups[0].text_event is None
    assert groups[0].upload_events == ()
