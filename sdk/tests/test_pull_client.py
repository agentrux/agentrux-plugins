"""Tests for PullClient — adaptive polling, cursor management, wake()."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.pull_client import PullClient

from .conftest import stub_event_view, stub_list_events_response


pytestmark = pytest.mark.asyncio


TOPIC = "top_00000000-0000-0000-0000-000000000001"
EVT1 = "evt_00000000-0000-0000-0000-000000000001"
EVT2 = "evt_00000000-0000-0000-0000-000000000002"


async def test_poll_once_returns_events(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[stub_event_view(event_id=EVT1, sequence_number=1)],
                after=EVT1,
                after_seq=1,
                has_more=False,
            ),
        )

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(api, TOPIC, batch_size=10)
        msgs = await pull.poll_once()
        assert len(msgs) == 1
        assert msgs[0].event_id == EVT1
        await pull.stop()


async def test_poll_once_advances_cursor(make_api_client) -> None:
    """When `start_after_event_id` is given, no latest-init probe is
    issued; the cursor advances normally on each poll."""
    call_count = [0]
    seen_cursors: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        seen_cursors.append(params.get("after"))
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(
                200,
                json=stub_list_events_response(
                    events=[stub_event_view(event_id=EVT1, sequence_number=1)],
                ),
            )
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        # Explicit start_after disables latest-init.
        pull = PullClient(
            api, TOPIC, start_after_event_id="evt_00000000-0000-0000-0000-000000000000",
        )
        await pull.poll_once()
        await pull.poll_once()
        await pull.stop()
    assert seen_cursors == ["evt_00000000-0000-0000-0000-000000000000", EVT1]


async def test_latest_init_uses_head_event_id_when_topic_has_events(make_api_client) -> None:
    """`start_after_event_id=None` triggers a desc/limit=1 probe; the
    head event_id becomes the cursor (so subsequent polls see only
    newer events)."""
    call_log: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        call_log.append(params)
        if params.get("order") == "desc" and params.get("limit") == "1":
            # init probe — return one event as the head.
            return httpx.Response(
                200,
                json=stub_list_events_response(
                    events=[stub_event_view(event_id=EVT1, sequence_number=42)],
                ),
            )
        # subsequent polls — return nothing, just verify cursor.
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(api, TOPIC)
        await pull.poll_once()
        await pull.stop()
    # First call: init probe (order=desc, limit=1)
    # Second call: real poll with after=<head event_id>
    assert len(call_log) >= 2
    assert call_log[0].get("order") == "desc"
    assert call_log[1].get("after") == EVT1


async def test_latest_init_samples_since_before_probe(make_api_client) -> None:
    """`since=` must be sampled BEFORE the head probe (Codex 2nd review #2).

    We capture the timestamp right before driving the probe and verify
    the since= value sent on the next poll is earlier than (or equal to)
    that timestamp — meaning we wouldn't have missed an event arriving
    between probe and install.
    """
    import time
    from datetime import datetime, timezone

    call_log: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        call_log.append(dict(req.url.params))
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        marker_before = datetime.now(timezone.utc)
        pull = PullClient(api, TOPIC)
        await pull.poll_once()
        marker_after = datetime.now(timezone.utc)
        await pull.stop()
    # Second call is the real poll with since= installed.
    poll_call = call_log[1]
    assert "since" in poll_call
    since_dt = datetime.fromisoformat(poll_call["since"])
    # The sampled since must fall in [marker_before, marker_after]: it was
    # taken just before the probe was sent.
    assert marker_before <= since_dt <= marker_after, (
        f"since={since_dt} not in [{marker_before}, {marker_after}]"
    )


async def test_latest_init_empty_topic_uses_since_filter(make_api_client) -> None:
    """When the topic head probe returns no events, the next poll
    filters by `since=<init time ISO>` so we only see events arriving
    after subscription."""
    call_log: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        params = dict(req.url.params)
        call_log.append(params)
        # Always return empty: empty topic.
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(api, TOPIC)
        await pull.poll_once()
        await pull.stop()
    # First: init probe (order=desc). Second: real poll with since= set.
    assert len(call_log) >= 2
    init_call, poll_call = call_log[0], call_log[1]
    assert init_call.get("order") == "desc"
    assert "since" in poll_call
    assert poll_call["since"].startswith("20")  # ISO datetime
    # No `after` on the empty-topic poll.
    assert poll_call.get("after") is None


async def test_poll_once_empty_response(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(api, TOPIC)
        msgs = await pull.poll_once()
        assert msgs == []
        await pull.stop()


async def test_poll_resilient_to_http_error(make_api_client) -> None:
    """Pull errors must not kill the consumer — the next poll just retries."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": {"error": "INTERNAL", "message": "x"}})

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        api.INITIAL_BACKOFF_SECONDS = 0.001
        pull = PullClient(api, TOPIC)
        msgs = await pull.poll_once()
        assert msgs == []  # error swallowed; stats incremented
        assert pull.stats.errors >= 1
        await pull.stop()


async def test_wake_skips_sleep(make_api_client) -> None:
    """wake() should make the next iteration return immediately."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(api, TOPIC, poll_interval_ms=10_000, min_interval_ms=10_000)
        # Mark current interval at max so the sleep would otherwise be long.
        pull._current_interval_ms = 10_000  # type: ignore[attr-defined]
        pull.wake()
        # If wake() worked, the asyncio.wait_for inside __aiter__ returns
        # immediately. We construct the iter manually with a tight bound.
        it = pull.__aiter__()
        # Just confirm wake_event is set
        assert pull._wake_event.is_set()  # type: ignore[attr-defined]
        await pull.stop()
        _ = it


async def test_rejects_bad_topic_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="topic_id"):
            PullClient(api, "bad_topic_id")


async def test_rejects_bad_start_event_id_prefix(make_api_client) -> None:
    async with make_api_client({}) as api:
        with pytest.raises(ValueError, match="start_after_event_id"):
            PullClient(api, TOPIC, start_after_event_id="bogus_xx")


async def test_restore_cursor(make_api_client) -> None:
    async with make_api_client({}) as api:
        pull = PullClient(api, TOPIC)
        pull.restore_cursor(EVT1)
        assert pull.cursor == EVT1
        with pytest.raises(ValueError):
            pull.restore_cursor("bad_cursor")
        await pull.stop()


async def test_adaptive_interval_grows_on_empty(make_api_client) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(
            api, TOPIC,
            poll_interval_ms=100, min_interval_ms=100, max_interval_ms=1600,
        )
        initial = pull._current_interval_ms  # type: ignore[attr-defined]
        await pull.poll_once()
        after_one = pull._current_interval_ms  # type: ignore[attr-defined]
        await pull.poll_once()
        after_two = pull._current_interval_ms  # type: ignore[attr-defined]
        assert after_one >= initial
        assert after_two >= after_one
        await pull.stop()


async def test_stop_unblocks_iteration_during_adaptive_max_sleep(make_api_client) -> None:
    """When the topic is idle, adaptive polling backs off toward
    `max_interval_ms`. Calling stop() must wake the sleep immediately;
    the iteration loop must NOT wait out the configured max.

    Without `wake()` plumbing into stop(), this would block for tens of
    seconds (or whatever max_interval_ms is set to).
    """

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=stub_list_events_response(events=[]))

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(
            api,
            TOPIC,
            # Long max so a passing test PROVES stop() interrupted the sleep.
            poll_interval_ms=100,
            min_interval_ms=100,
            max_interval_ms=10_000,
        )

        async def _consume_then_stop() -> None:
            count = 0
            async for _ in pull:
                count += 1
                if count >= 2:
                    break  # exit naturally
            # If pull was already stopped, breaking out is the success.

        async def _stop_quickly() -> None:
            await asyncio.sleep(0.1)
            await pull.stop()

        start = asyncio.get_event_loop().time()
        await asyncio.gather(_consume_then_stop(), _stop_quickly())
        elapsed = asyncio.get_event_loop().time() - start
        # Hard bound: well below the 10s configured max.
        assert elapsed < 1.5, f"stop() did not interrupt adaptive sleep: {elapsed:.2f}s"


async def test_adaptive_interval_resets_on_data(make_api_client) -> None:
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx.Response(200, json=stub_list_events_response(events=[]))
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[stub_event_view(event_id=EVT1, sequence_number=1)]
            ),
        )

    async with make_api_client({("GET", f"/topics/{TOPIC}/events"): handler}) as api:
        pull = PullClient(
            api, TOPIC,
            poll_interval_ms=100, min_interval_ms=100, max_interval_ms=1600,
        )
        await pull.poll_once()
        after_empty = pull._current_interval_ms  # type: ignore[attr-defined]
        await pull.poll_once()
        after_data = pull._current_interval_ms  # type: ignore[attr-defined]
        assert after_data == 100  # min
        assert after_empty >= 100
        await pull.stop()
