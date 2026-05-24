"""Tests for GapDetector — best-effort backfill via list_events(after=, limit=).

Behavior:
- With `before_event_id=None` (no anchor): full range is unrecoverable
  immediately (no API call).
- With `before_event_id` set + api_client: probe `list_events(after=, limit=)`
  and reinject any seqs that fall within the gap range; report any
  residual missing seqs as unrecoverable.
- Without callback registered AND no anchor: GapUnrecoverableError raised.
"""
from __future__ import annotations

import httpx
import pytest

from agentrux.sdk.client import AgenTruxAPIClient, TokenManager
from agentrux.sdk.errors import GapUnrecoverableError
from agentrux.sdk.gap_detector import FillResult, GapDetector

from .conftest import stub_event_view, stub_list_events_response


pytestmark = pytest.mark.asyncio


def _make_api(handler):
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.agentrux.test")
    tm = TokenManager(access_token="aat_test")
    return AgenTruxAPIClient(
        base_url="https://api.agentrux.test", token_manager=tm, http=http
    )


EVT_ANCHOR = "evt_00000000-0000-0000-0000-000000000010"


# ---------- no-anchor: instant unrecoverable -------------------------


async def test_gap_with_no_anchor_marks_unrecoverable_no_exception() -> None:
    captured = []

    async def on_unrec(start: int, end: int, reason: str) -> None:
        captured.append((start, end, reason))

    gd = GapDetector(on_unrecoverable=on_unrec)
    result = await gd.fill("top_a", 5, 10)  # no before_event_id
    assert result.backfilled == []
    assert result.missing_ranges == [(5, 10)]
    assert captured == [(5, 10, "no_anchor")]


async def test_gap_no_anchor_without_callback_raises() -> None:
    gd = GapDetector()
    with pytest.raises(GapUnrecoverableError) as ei:
        await gd.fill("top_a", 100, 105)
    assert ei.value.gap_start_seq == 100
    assert ei.value.gap_end_seq == 105


# ---------- with anchor: full / partial / no fill --------------------


async def test_gap_full_fill_via_list_events() -> None:
    """All seqs in [5, 7] are returned by the probe → backfilled, no missing."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[
                    stub_event_view(
                        event_id=f"evt_00000000-0000-0000-0000-00000000000{i}",
                        sequence_number=i,
                    )
                    for i in (5, 6, 7)
                ],
            ),
        )

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        gd = GapDetector(api)
        result = await gd.fill("top_a", 5, 7, before_event_id=EVT_ANCHOR)
        assert len(result.backfilled) == 3
        assert [m.sequence_number for m in result.backfilled] == [5, 6, 7]
        assert result.missing_ranges == []
    finally:
        await api.close()


async def test_gap_partial_fill_residual_missing_reported() -> None:
    """Probe returns 5 and 7 (server skipped 6 — never produced) →
    missing_ranges=[(6, 6)]."""
    captured = []

    async def on_unrec(start: int, end: int, reason: str) -> None:
        captured.append((start, end, reason))

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000005",
                        sequence_number=5,
                    ),
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000007",
                        sequence_number=7,
                    ),
                ],
            ),
        )

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        gd = GapDetector(api, on_unrecoverable=on_unrec)
        result = await gd.fill("top_a", 5, 7, before_event_id=EVT_ANCHOR)
        assert [m.sequence_number for m in result.backfilled] == [5, 7]
        assert result.missing_ranges == [(6, 6)]
        assert captured == [(6, 6, "partial_fill")]
    finally:
        await api.close()


async def test_gap_probe_returns_unrelated_seqs_only() -> None:
    """Probe returns 8, 9 (outside the gap [5, 7]) →
    nothing backfilled, full range unrecoverable."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000008",
                        sequence_number=8,
                    ),
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000009",
                        sequence_number=9,
                    ),
                ],
            ),
        )

    captured = []

    async def on_unrec(start: int, end: int, reason: str) -> None:
        captured.append((start, end, reason))

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        gd = GapDetector(api, on_unrecoverable=on_unrec)
        result = await gd.fill("top_a", 5, 7, before_event_id=EVT_ANCHOR)
        assert result.backfilled == []
        assert result.missing_ranges == [(5, 7)]
        assert captured == [(5, 7, "no_events_in_probe")]
    finally:
        await api.close()


async def test_gap_probe_http_error_is_unrecoverable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": {"error": "INTERNAL", "message": "x"}})

    captured = []

    async def on_unrec(start: int, end: int, reason: str) -> None:
        captured.append((start, end, reason))

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        api.INITIAL_BACKOFF_SECONDS = 0.001
        gd = GapDetector(api, on_unrecoverable=on_unrec)
        result = await gd.fill("top_a", 1, 3, before_event_id=EVT_ANCHOR)
        assert result.missing_ranges == [(1, 3)]
        assert captured == [(1, 3, "probe_failed")]
    finally:
        await api.close()


# ---------- callback isolation ---------------------------------------


async def test_callback_exception_is_isolated() -> None:
    """A user callback that raises must NOT crash the detector."""

    async def bad_cb(start: int, end: int, reason: str) -> None:
        raise RuntimeError("user oops")

    gd = GapDetector(on_unrecoverable=bad_cb)
    result = await gd.fill("top_a", 1, 2)  # no anchor → unrecoverable path
    assert result.missing_ranges == [(1, 2)]


# ---------- stats ----------------------------------------------------


async def test_stats_count_detected_and_unrecoverable() -> None:
    async def noop(s: int, e: int, r: str) -> None:
        return None

    gd = GapDetector(on_unrecoverable=noop)
    await gd.fill("top_a", 1, 5)
    await gd.fill("top_a", 100, 200)
    s = gd.stats
    assert s.gaps_detected == 2
    assert s.gaps_unrecoverable == 2


# ---------- multi-page probe (Codex 2nd review #3) -------------------


async def test_gap_paginates_until_range_covered() -> None:
    """A wide gap that spans more than one page must be fetched across
    multiple list_events calls, following next.has_more."""
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        params = dict(req.url.params)
        after = params.get("after")
        # Page 1: anchor → events 5,6 with has_more=True
        if call_count[0] == 1:
            assert after == EVT_ANCHOR
            return httpx.Response(
                200,
                json=stub_list_events_response(
                    events=[
                        stub_event_view(
                            event_id="evt_00000000-0000-0000-0000-000000000005",
                            sequence_number=5,
                        ),
                        stub_event_view(
                            event_id="evt_00000000-0000-0000-0000-000000000006",
                            sequence_number=6,
                        ),
                    ],
                    after="evt_00000000-0000-0000-0000-000000000006",
                    after_seq=6,
                    has_more=True,
                ),
            )
        # Page 2: next.after=evt_6 → events 7,8 with has_more=False
        assert after == "evt_00000000-0000-0000-0000-000000000006"
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000007",
                        sequence_number=7,
                    ),
                    stub_event_view(
                        event_id="evt_00000000-0000-0000-0000-000000000008",
                        sequence_number=8,
                    ),
                ],
            ),
        )

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        # max_probe_events=2 forces pagination for the gap [5,7].
        gd = GapDetector(api, max_probe_events=2)
        result = await gd.fill(
            "top_a", 5, 7, before_event_id=EVT_ANCHOR,
        )
        assert call_count[0] == 2  # two pages fetched
        assert [m.sequence_number for m in result.backfilled] == [5, 6, 7]
        assert result.missing_ranges == []
    finally:
        await api.close()


async def test_gap_pagination_stops_at_max_pages_safety_cap() -> None:
    """If a server keeps saying has_more=True forever AND keeps
    returning events that fall *below* the gap range, the probe must
    stop at MAX_PROBE_PAGES rather than loop forever."""
    call_count = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        # Each page returns one event with seq=call_count[0] (always
        # below the gap [100, 200]), so `last_seq >= end_seq` never
        # triggers and the only termination is the page cap.
        seq = call_count[0]
        return httpx.Response(
            200,
            json=stub_list_events_response(
                events=[
                    stub_event_view(
                        event_id=f"evt_00000000-0000-0000-0000-{seq:012d}",
                        sequence_number=seq,
                    )
                ],
                after=f"evt_00000000-0000-0000-0000-{seq:012d}",
                after_seq=seq,
                has_more=True,
            ),
        )

    api = _make_api(
        lambda req: handler(req)
        if (req.method, req.url.path) == ("GET", "/topics/top_a/events")
        else httpx.Response(404)
    )
    try:
        gd = GapDetector(api, max_probe_events=1)
        await gd.fill("top_a", 100, 200, before_event_id=EVT_ANCHOR)
        # Loop body runs exactly MAX_PROBE_PAGES times (pages=0..15
        # incrementing to 16 after which `pages < 16` is False).
        assert call_count[0] == GapDetector.MAX_PROBE_PAGES, (
            f"expected exactly MAX_PROBE_PAGES calls, got {call_count[0]}"
        )
    finally:
        await api.close()
