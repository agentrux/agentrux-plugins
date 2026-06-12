"""SDK Phase 5.6 — pipeline + checkpoint + gap detector + reorder buffer."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agentrux_sdk import AgenTruxClient
from agentrux_sdk.checkpoint import FileCheckpointStore, InMemoryCheckpointStore
from agentrux_sdk.errors import GapDetectedError, ValidationError
from agentrux_sdk.gap_detector import SequenceGapDetector
from agentrux_sdk.models import Event
from agentrux_sdk.reorder_buffer import ReorderBuffer

pytestmark = pytest.mark.unit


# ============================================================================
# CheckpointStore (in-memory + file)
# ============================================================================


@pytest.mark.asyncio
async def test_in_memory_checkpoint_round_trip() -> None:
    cp = InMemoryCheckpointStore()
    assert await cp.load("top_a") is None
    await cp.commit("top_a", "evt_001")
    assert await cp.load("top_a") == "evt_001"
    await cp.commit("top_a", "evt_002")  # 上書き
    assert await cp.load("top_a") == "evt_002"


@pytest.mark.asyncio
async def test_file_checkpoint_persists_across_instances(tmp_path) -> None:
    p = tmp_path / "cp.json"
    cp1 = FileCheckpointStore(p)
    await cp1.commit("top_a", "evt_x")
    cp2 = FileCheckpointStore(p)
    assert await cp2.load("top_a") == "evt_x"


# ============================================================================
# SequenceGapDetector
# ============================================================================


def test_gap_detector_in_order_passes() -> None:
    gd = SequenceGapDetector(window=10)
    for i in [1, 2, 3, 4, 5]:
        gd.observe(i, topic_id="top_x")
    assert gd.last_seen == 5


def test_gap_detector_small_gap_within_window_passes() -> None:
    """gap が window 以内 → reorder buffer 解決と仮定して pass."""
    gd = SequenceGapDetector(window=10)
    gd.observe(1, topic_id="top_x")
    gd.observe(5, topic_id="top_x")  # gap=3 < window
    assert gd.last_seen == 5


def test_gap_detector_large_gap_raises() -> None:
    gd = SequenceGapDetector(window=5)
    gd.observe(10, topic_id="top_x")
    with pytest.raises(GapDetectedError) as ei:
        gd.observe(100, topic_id="top_x")  # gap=89 > 5
    assert ei.value.gap_after == 10
    assert ei.value.gap_size == 89


def test_gap_detector_duplicate_ignored() -> None:
    gd = SequenceGapDetector(window=5)
    gd.observe(5, topic_id="top_x")
    gd.observe(3, topic_id="top_x")  # < last_seen → 無視
    gd.observe(5, topic_id="top_x")  # 同 → 無視
    assert gd.last_seen == 5


# ============================================================================
# ReorderBuffer
# ============================================================================


@pytest.mark.asyncio
async def test_reorder_buffer_immediate_flush_in_order() -> None:
    buf = ReorderBuffer(max_lag=10)
    out1 = await buf.push(sequence_number=1, event="a")
    out2 = await buf.push(sequence_number=2, event="b")
    assert out1 == ["a"]
    assert out2 == ["b"]
    assert buf.pending_count == 0


@pytest.mark.asyncio
async def test_reorder_buffer_out_of_order_buffered_then_flushed() -> None:
    buf = ReorderBuffer(max_lag=10)
    out1 = await buf.push(sequence_number=1, event="a")  # → ["a"]
    out3 = await buf.push(sequence_number=3, event="c")  # buffer (2 待ち)
    out2 = await buf.push(sequence_number=2, event="b")  # → ["b", "c"] (2,3 連続)
    assert out1 == ["a"]
    assert out3 == []
    assert out2 == ["b", "c"]
    assert buf.pending_count == 0


@pytest.mark.asyncio
async def test_reorder_buffer_max_lag_forces_flush() -> None:
    """max_lag=2、 5→7→9 と来たら 9 で max_lag 超過 → 5 を _next にして 5,7,9 を順次 flush."""
    buf = ReorderBuffer(max_lag=2)
    out5 = await buf.push(sequence_number=5, event="e5")  # _next=5 → ["e5"]
    out7 = await buf.push(sequence_number=7, event="e7")  # 待ち、 pending=1
    out9 = await buf.push(sequence_number=9, event="e9")  # 待ち、 pending=2 (OK)
    out11 = await buf.push(sequence_number=11, event="e11")  # 待ち、 pending=3 (>max_lag=2 → 諦め)
    # max_lag 超過時に _next を最小 (7) に進めて 7 を flush。 9, 11 は再び pending
    assert out5 == ["e5"]
    assert out7 == []
    assert out9 == []
    assert out11 == ["e7"]


@pytest.mark.asyncio
async def test_reorder_buffer_duplicate_ignored() -> None:
    buf = ReorderBuffer(max_lag=10)
    o1 = await buf.push(sequence_number=1, event="a")
    o1_dup = await buf.push(sequence_number=1, event="a-dup")  # 古い → 無視
    assert o1 == ["a"]
    assert o1_dup == []


# ============================================================================
# Pipeline (end-to-end with mock transport)
# ============================================================================


def _make_client_for_pipeline(handler: callable) -> AgenTruxClient:
    client = AgenTruxClient(
        endpoint="https://api.example.com",
        client_id="crd_pipe",
        client_secret="aks_pipe",
    )
    client._http._client = httpx.AsyncClient(
        base_url=client.config.endpoint,
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": client.config.user_agent},
    )
    return client


@pytest.mark.asyncio
async def test_pipeline_runs_transform_publishes_to_sink() -> None:
    """source→transform→sink の round-trip + checkpoint commit verify."""
    state = {"published": [], "tx_calls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        if req.method == "GET" and req.url.path == "/topics/top_src/events":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_id": "evt_001",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "sequence_number": 1,
                            "stored_at": "2026-05-17T00:00:00+00:00",
                            "payload": {"n": 1},
                        },
                        {
                            "event_id": "evt_002",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "sequence_number": 2,
                            "stored_at": "2026-05-17T00:00:01+00:00",
                            "payload": {"n": 2},
                        },
                    ],
                    "next": {"has_more": False},
                },
            )
        if req.method == "POST" and req.url.path == "/topics/top_sink/events":
            import json

            body = json.loads(req.content)
            state["published"].append((req.headers.get("idempotency-key"), body["payload"]))
            return httpx.Response(
                201,
                json={
                    "event_id": f"evt_out_{len(state['published'])}",
                    "sequence_number": len(state["published"]),
                },
            )
        return httpx.Response(500, text=f"unexpected: {req.method} {req.url.path}")

    async def transform(evt: Event) -> dict:
        state["tx_calls"] += 1
        return {"out": evt.payload["n"] * 10}

    client = _make_client_for_pipeline(handler)
    cp = InMemoryCheckpointStore()
    try:
        pipe = client.pipeline(
            source_topic="top_src",
            sink_topic="top_sink",
            transform=transform,
            checkpoint_store=cp,
            pull_interval_seconds=0.01,
        )
        processed = await pipe.run(max_events=2)
        assert processed == 2
        assert state["tx_calls"] == 2
        assert len(state["published"]) == 2
        # idempotency_key は source event_id を継承
        assert state["published"][0][0] == "idk_pipe_evt_001"
        assert state["published"][0][1] == {"out": 10}
        assert state["published"][1][1] == {"out": 20}
        # checkpoint は最後の処理成功 source event_id
        assert await cp.load("top_src") == "evt_002"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pipeline_filter_transform_returns_none_skips_publish() -> None:
    """transform 戻り値 None → publish せず checkpoint だけ進む."""
    published: list = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_f", "token_type": "Bearer", "expires_in": 600}
            )
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_id": "evt_x",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "sequence_number": 5,
                            "stored_at": "2026-05-17T00:00:00+00:00",
                            "payload": {"keep": False},
                        }
                    ],
                    "next": {"has_more": False},
                },
            )
        published.append(req.url.path)
        return httpx.Response(201, json={"event_id": "evt_o", "sequence_number": 1})

    async def transform(evt: Event):
        return None  # filter out

    client = _make_client_for_pipeline(handler)
    cp = InMemoryCheckpointStore()
    try:
        pipe = client.pipeline(
            source_topic="top_src",
            sink_topic="top_sink",
            transform=transform,
            checkpoint_store=cp,
            pull_interval_seconds=0.01,
        )
        # max_events=0 だと開始即 return → 1 件目で停止しないので、 transform 後の publish スキップを確認するため
        # max_events=1 で stop しない設定にして、 transform 後の publish 0 件を確認
        # → filter なら processed=0 のまま、 stop しないので tail-loop に入る。 timeout で打ち切り。
        try:
            await asyncio.wait_for(pipe.run(max_events=1), timeout=0.5)
        except TimeoutError:
            pass
        assert published == []  # publish 起きていない
        assert await cp.load("top_src") == "evt_x"  # checkpoint だけ進んだ
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pipeline_validates_topic_prefix() -> None:
    client = _make_client_for_pipeline(lambda req: httpx.Response(500))
    try:
        with pytest.raises(ValidationError):
            client.pipeline(
                source_topic="invalid",
                sink_topic="top_y",
                transform=lambda e: None,
            )
        with pytest.raises(ValidationError):
            client.pipeline(
                source_topic="top_x",
                sink_topic="invalid",
                transform=lambda e: None,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pipeline_rejects_invalid_mode() -> None:
    client = _make_client_for_pipeline(lambda req: httpx.Response(500))
    try:
        with pytest.raises(ValidationError, match="mode"):
            client.pipeline(
                source_topic="top_x",
                sink_topic="top_y",
                transform=lambda e: None,
                mode="weird",
            )
    finally:
        await client.aclose()
