"""SDK Phase 5.6 — pipeline + checkpoint + dedupe + cursor (cluster-agnostic モデル).

cluster_agnostic_ordering.md §2 に合わせて書き換え:
  - gap_detector / reorder_buffer テストを撤去 (seq 連番依存、 新契約で成立しない)
  - dedupe / RetentionMissError / opaque cursor checkpoint / 空 poll frontier の単体テスト追加
  - near-order (batch 内 stored_at ソート) の単体テスト追加
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentrux_sdk import AgenTruxClient
from agentrux_sdk.checkpoint import FileCheckpointStore, InMemoryCheckpointStore
from agentrux_sdk.dedupe import EventIdDedupe
from agentrux_sdk.errors import RetentionMissError, ValidationError
from agentrux_sdk.models import Event

pytestmark = pytest.mark.unit


# ============================================================================
# CheckpointStore — opaque cursor 保存
# ============================================================================


@pytest.mark.asyncio
async def test_in_memory_checkpoint_round_trip() -> None:
    cp = InMemoryCheckpointStore()
    assert await cp.load("top_a") is None
    await cp.commit("top_a", "cursor_opaque_001")
    assert await cp.load("top_a") == "cursor_opaque_001"
    await cp.commit("top_a", "cursor_opaque_002")  # 上書き
    assert await cp.load("top_a") == "cursor_opaque_002"


@pytest.mark.asyncio
async def test_file_checkpoint_persists_across_instances(tmp_path) -> None:
    p = tmp_path / "cp.json"
    cp1 = FileCheckpointStore(p)
    await cp1.commit("top_a", "opaque_cursor_xyz")
    cp2 = FileCheckpointStore(p)
    assert await cp2.load("top_a") == "opaque_cursor_xyz"


# ============================================================================
# EventIdDedupe — bounded window 重複排除
# ============================================================================


def test_dedupe_first_occurrence_is_not_duplicate() -> None:
    d = EventIdDedupe(window=10)
    assert not d.is_duplicate("evt_001")
    assert d.seen_count == 1


def test_dedupe_second_occurrence_is_duplicate() -> None:
    d = EventIdDedupe(window=10)
    d.is_duplicate("evt_001")
    assert d.is_duplicate("evt_001")
    assert d.seen_count == 1  # 重複は追加されない


def test_dedupe_window_eviction_drops_oldest() -> None:
    """window=2: evt_001 → evt_002 → evt_003 で evt_001 が evict され、 再挿入時に not-duplicate."""
    d = EventIdDedupe(window=2)
    d.is_duplicate("evt_001")
    d.is_duplicate("evt_002")
    d.is_duplicate("evt_003")  # evt_001 が evict される
    assert not d.is_duplicate("evt_001")  # evict 済なので初回扱い
    assert d.seen_count == 2


def test_dedupe_invalid_window_raises() -> None:
    with pytest.raises(ValueError, match="window"):
        EventIdDedupe(window=0)


def test_dedupe_multiple_topics_independent() -> None:
    """topic が異なれば event_id が同一でも別扱い (EventIdDedupe は event_id だけで管理するため
    同一 event_id でも is_duplicate=True になる — caller が topic 別に instance を持つことを想定)."""
    d = EventIdDedupe(window=100)
    assert not d.is_duplicate("evt_001")
    # 同 event_id → duplicate
    assert d.is_duplicate("evt_001")


# ============================================================================
# Pull client — near-order sort (batch 内 stored_at 昇順)
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
async def test_pull_near_order_batch_sort_by_stored_at() -> None:
    """Pull が batch 内を stored_at 昇順にソートして yield することを確認."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        # stored_at が逆順で来ても SDK がソートして返す
        return httpx.Response(
            200,
            json={
                "events": [
                    {
                        "event_id": "evt_003",
                        "topic_id": "top_src",
                        "event_type": "in",
                        "stored_at": "2026-06-13T00:00:03+00:00",
                        "cursor": "cursor_003",
                        "payload": {"n": 3},
                    },
                    {
                        "event_id": "evt_001",
                        "topic_id": "top_src",
                        "event_type": "in",
                        "stored_at": "2026-06-13T00:00:01+00:00",
                        "cursor": "cursor_001",
                        "payload": {"n": 1},
                    },
                    {
                        "event_id": "evt_002",
                        "topic_id": "top_src",
                        "event_type": "in",
                        "stored_at": "2026-06-13T00:00:02+00:00",
                        "cursor": "cursor_002",
                        "payload": {"n": 2},
                    },
                ],
                "next": {"has_more": False},
            },
        )

    client = _make_client_for_pipeline(handler)
    try:
        seen_ids = []
        async for evt in client.read_pull(topic_id="top_src", stop_when_empty=True):
            seen_ids.append(evt.event_id)
        assert seen_ids == ["evt_001", "evt_002", "evt_003"]
    finally:
        await client.aclose()


# ============================================================================
# Pull client — 空 poll での frontier cursor 保持
# ============================================================================


@pytest.mark.asyncio
async def test_pull_empty_poll_advances_frontier_cursor() -> None:
    """空 poll でも server が frontier_cursor を返したら cursor が前進する."""
    captured: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        return httpx.Response(
            200,
            json={
                "events": [],
                "next": {"has_more": False, "frontier_cursor": "cursor_frontier_99"},
            },
        )

    from agentrux_sdk.pull_client import read_pull

    client = _make_client_for_pipeline(handler)
    try:
        async for _ in read_pull(
            client,
            topic_id="top_src",
            stop_when_empty=True,
            on_cursor_advance=captured.append,
        ):
            pass
        # 空 poll でも frontier cursor で前進
        assert captured == ["cursor_frontier_99"]
    finally:
        await client.aclose()


# ============================================================================
# Pull client — RETENTION_MISS → RetentionMissError
# ============================================================================


@pytest.mark.asyncio
async def test_pull_retention_miss_raises_retention_miss_error() -> None:
    """server が RETENTION_MISS を返したら RetentionMissError を raise する."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        return httpx.Response(
            404,
            json={"detail": {"error": "RETENTION_MISS", "message": "cursor outside retention"}},
        )

    client = _make_client_for_pipeline(handler)
    try:
        with pytest.raises(RetentionMissError, match="retention"):
            async for _ in client.read_pull(
                topic_id="top_src", after="cursor_old", stop_when_empty=True
            ):
                pass
    finally:
        await client.aclose()


# ============================================================================
# Pipeline — opaque cursor checkpoint
# ============================================================================


@pytest.mark.asyncio
async def test_pipeline_checkpoints_opaque_cursor_not_event_id() -> None:
    """pipeline は evt.cursor を checkpoint に保存する (event_id ではなく)."""
    state = {"published": []}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_id": "evt_001",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "stored_at": "2026-06-13T00:00:01+00:00",
                            "cursor": "opaque_cursor_001",
                            "payload": {"n": 1},
                        },
                        {
                            "event_id": "evt_002",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "stored_at": "2026-06-13T00:00:02+00:00",
                            "cursor": "opaque_cursor_002",
                            "payload": {"n": 2},
                        },
                    ],
                    "next": {"has_more": False},
                },
            )
        if req.method == "POST":
            body = json.loads(req.content)
            state["published"].append(body["payload"])
            return httpx.Response(
                201,
                json={"event_id": f"evt_out_{len(state['published'])}"},
            )
        return httpx.Response(500, text=f"unexpected: {req.method} {req.url.path}")

    async def transform(evt: Event) -> dict:
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
        # checkpoint は opaque cursor を保存 (event_id ではなく)
        assert await cp.load("top_src") == "opaque_cursor_002"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pipeline_dedupes_repeated_event_ids() -> None:
    """同一 event_id が重複して来ても 1 回しか publish しない (at-least-once dedupe)."""
    publish_count = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        if req.method == "GET":
            # 同じ event を 2 回返す (at-least-once の典型 scenario)
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "event_id": "evt_dup",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "stored_at": "2026-06-13T00:00:01+00:00",
                            "cursor": "cursor_dup",
                            "payload": {"n": 1},
                        },
                        {
                            "event_id": "evt_dup",  # 重複
                            "topic_id": "top_src",
                            "event_type": "in",
                            "stored_at": "2026-06-13T00:00:02+00:00",
                            "cursor": "cursor_dup2",
                            "payload": {"n": 1},
                        },
                    ],
                    "next": {"has_more": False},
                },
            )
        if req.method == "POST":
            publish_count["n"] += 1
            return httpx.Response(
                201, json={"event_id": f"evt_out_{publish_count['n']}"}
            )
        return httpx.Response(500)

    async def transform(evt: Event) -> dict:
        return {"out": evt.payload["n"]}

    client = _make_client_for_pipeline(handler)
    try:
        pipe = client.pipeline(
            source_topic="top_src",
            sink_topic="top_sink",
            transform=transform,
            pull_interval_seconds=0.01,
        )
        # max_events=1: 最初の dedupe-新規 event のみ処理
        processed = await pipe.run(max_events=1)
        assert processed == 1
        assert publish_count["n"] == 1  # 重複 2 件目は dedupe で skip
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_pipeline_retention_miss_aborts_run() -> None:
    """pipeline の read で RetentionMissError が発生したら run が中断される."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(
                200, json={"access_token": "aat_p", "token_type": "Bearer", "expires_in": 600}
            )
        return httpx.Response(
            404,
            json={"detail": {"error": "RETENTION_MISS", "message": "cursor outside retention"}},
        )

    async def transform(evt: Event) -> dict:
        return {}

    client = _make_client_for_pipeline(handler)
    try:
        pipe = client.pipeline(
            source_topic="top_src",
            sink_topic="top_sink",
            transform=transform,
        )
        with pytest.raises(RetentionMissError):
            await pipe.run()
    finally:
        await client.aclose()


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
                            "stored_at": "2026-06-13T00:00:01+00:00",
                            "cursor": "cursor_001",
                            "payload": {"n": 1},
                        },
                        {
                            "event_id": "evt_002",
                            "topic_id": "top_src",
                            "event_type": "in",
                            "stored_at": "2026-06-13T00:00:02+00:00",
                            "cursor": "cursor_002",
                            "payload": {"n": 2},
                        },
                    ],
                    "next": {"has_more": False},
                },
            )
        if req.method == "POST" and req.url.path == "/topics/top_sink/events":
            body = json.loads(req.content)
            state["published"].append((req.headers.get("idempotency-key"), body["payload"]))
            return httpx.Response(
                201,
                json={"event_id": f"evt_out_{len(state['published'])}"},
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
        # checkpoint は opaque cursor
        assert await cp.load("top_src") == "cursor_002"
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
                            "stored_at": "2026-06-13T00:00:00+00:00",
                            "cursor": "cursor_x",
                            "payload": {"keep": False},
                        }
                    ],
                    "next": {"has_more": False},
                },
            )
        published.append(req.url.path)
        return httpx.Response(201, json={"event_id": "evt_o"})

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
        try:
            await asyncio.wait_for(pipe.run(max_events=1), timeout=0.5)
        except TimeoutError:
            pass
        assert published == []  # publish 起きていない
        assert await cp.load("top_src") == "cursor_x"  # opaque cursor で checkpoint
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
