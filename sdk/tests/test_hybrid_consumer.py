"""Smoke tests for HybridConsumer.

The deep integration of SSE wake → Pull immediate fetch is hard to test
deterministically without a real event loop race; we keep these tests
focused on construction, mode reporting, and shutdown.
"""
from __future__ import annotations

import httpx
import pytest

from agentrux.sdk.client import AgenTruxAPIClient, TokenManager
from agentrux.sdk.hybrid_consumer import HybridConsumer


pytestmark = pytest.mark.asyncio


TOPIC = "top_00000000-0000-0000-0000-000000000001"


def _make_api():
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    http = httpx.AsyncClient(transport=transport, base_url="https://api.agentrux.test")
    tm = TokenManager(access_token="aat_test")
    return AgenTruxAPIClient(
        base_url="https://api.agentrux.test", token_manager=tm, http=http
    )


async def test_hybrid_mode_default() -> None:
    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=True)
        assert hc.mode == "hybrid"
        await hc.stop()
    finally:
        await api.close()


async def test_pull_only_mode_when_sse_disabled() -> None:
    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=False)
        assert hc.mode == "pull"
        await hc.stop()
    finally:
        await api.close()


async def test_stop_is_idempotent() -> None:
    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=False)
        await hc.stop()
        await hc.stop()  # second call should not raise
    finally:
        await api.close()


async def test_stats_property_delegates_to_pull() -> None:
    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=False)
        s = hc.stats
        assert s.current_mode == "pull"
        await hc.stop()
    finally:
        await api.close()


async def test_repr_includes_topic_and_mode() -> None:
    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=False)
        s = repr(hc)
        assert "HybridConsumer" in s
        assert TOPIC in s
        await hc.stop()
    finally:
        await api.close()


async def test_resync_surfaces_through_iterator() -> None:
    """When the SSE side raises ResyncRequiredError (no user callback
    registered), the hybrid iterator must surface it on the next yield
    boundary — silently dropping the SSE failure while the pull keeps
    going would strand the consumer at a dead cursor (impl review #4).
    """
    import asyncio
    from agentrux.sdk.errors import ResyncRequiredError
    from agentrux.sdk.sse_client import ResyncFrame

    api = _make_api()
    try:
        hc = HybridConsumer(api, TOPIC, sse_enabled=True)
        # Inject a ResyncRequiredError into the hybrid's fatal slot
        # the same way _run_sse would; the iterator should pick it up.
        hc._fatal_sse_error = ResyncRequiredError(
            "test resync",
            reason="cursor_invalid",
            request_id="req_test",
            resume_via="latest",
            max_catchup=None,
        )
        await hc._pull.stop()
        # Drive the iterator briefly with a tiny timeout — the first
        # check inside __aiter__ raises before any yield.
        with pytest.raises(ResyncRequiredError):
            async for _ in hc:
                pass
    finally:
        await api.close()
