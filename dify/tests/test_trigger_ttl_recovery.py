"""Tests for the agentrux-trigger ttl_expired cursor recovery (v1.0.8).

Two surfaces:
  1. provider.agentrux_api.is_ttl_expired_cursor — pure detection of the
     pipe_router 404 ttl_expired-cursor signal (no dify runtime needed).
  2. new_event._on_event — the else-branch catch-up that re-anchors to
     skip-to-latest when the pinned cursor has aged out of retention.

dify_plugin / werkzeug are not installed in CI here, so we install minimal
stubs for the trigger interfaces the module imports at load time.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SRC = Path(__file__).resolve().parents[1] / "src-trigger"
sys.path.insert(0, str(SRC))


def _install_stubs() -> None:
    if "werkzeug" not in sys.modules:
        wz = types.ModuleType("werkzeug")
        wz.Request = type("Request", (), {})
        sys.modules["werkzeug"] = wz

    class Variables:
        def __init__(self, variables):
            self.variables = variables

    class EventIgnoreError(Exception):
        pass

    class Event:
        pass

    mods = {
        "dify_plugin": types.ModuleType("dify_plugin"),
        "dify_plugin.entities": types.ModuleType("dify_plugin.entities"),
        "dify_plugin.entities.trigger": types.ModuleType("dify_plugin.entities.trigger"),
        "dify_plugin.errors": types.ModuleType("dify_plugin.errors"),
        "dify_plugin.errors.trigger": types.ModuleType("dify_plugin.errors.trigger"),
        "dify_plugin.interfaces": types.ModuleType("dify_plugin.interfaces"),
        "dify_plugin.interfaces.trigger": types.ModuleType("dify_plugin.interfaces.trigger"),
    }
    mods["dify_plugin.entities.trigger"].Variables = Variables
    mods["dify_plugin.errors.trigger"].EventIgnoreError = EventIgnoreError
    mods["dify_plugin.interfaces.trigger"].Event = Event
    sys.modules.update(mods)


_install_stubs()

from provider.agentrux_api import HttpError, is_ttl_expired_cursor  # noqa: E402
from dify_plugin.errors.trigger import EventIgnoreError  # noqa: E402
from trigger.events.new_event import new_event as ne  # noqa: E402


def _ttl_body(oldest: str | None = "evt_1d044714") -> str:
    """Exact shape of pipe_router._ttl_expired_cursor_response (FastAPI wraps it
    under `detail`)."""
    return json.dumps({
        "detail": {
            "error": "NOT_FOUND",
            "message": "after cursor refers to a TTL-expired event",
            "details": {"reason": "ttl_expired", "oldest_available_evt_id": oldest},
            "next_action": "cursor_advance",
        }
    })


# ---------------------------------------------------------------------------
# 1. is_ttl_expired_cursor — detection (a 正常 / c 境界 / d 異常入力)
# ---------------------------------------------------------------------------

def test_detects_ttl_via_reason():
    assert is_ttl_expired_cursor(HttpError(404, _ttl_body())) is True


def test_detects_ttl_via_next_action_only():
    body = json.dumps({"detail": {"next_action": "cursor_advance"}})
    assert is_ttl_expired_cursor(HttpError(404, body)) is True


def test_detects_when_body_not_detail_wrapped():
    # forward-compat: signal at top level (no `detail` envelope)
    body = json.dumps({"details": {"reason": "ttl_expired"}})
    assert is_ttl_expired_cursor(HttpError(404, body)) is True


def test_rejects_404_without_ttl_signal():
    body = json.dumps({"detail": {"error": "NOT_FOUND", "message": "topic missing"}})
    assert is_ttl_expired_cursor(HttpError(404, body)) is False


def test_rejects_other_reason_even_with_cursor_advance():
    # next_action must NOT override a different reason (Codex #1 tightening).
    body = json.dumps({"detail": {"details": {"reason": "topic_deleted"},
                                  "next_action": "cursor_advance"}})
    assert is_ttl_expired_cursor(HttpError(404, body)) is False


@pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 500, 503])
def test_rejects_non_404_even_with_ttl_body(status):
    # A ttl_expired-shaped body on a non-404 must not trigger re-anchor.
    assert is_ttl_expired_cursor(HttpError(status, _ttl_body())) is False


def test_rejects_non_http_error():
    assert is_ttl_expired_cursor(ValueError("boom")) is False
    assert is_ttl_expired_cursor(RuntimeError()) is False


@pytest.mark.parametrize("body", ["", "not json", "<html>404</html>", "[]", "null", "123"])
def test_rejects_malformed_or_non_object_body(body):
    assert is_ttl_expired_cursor(HttpError(404, body)) is False


# ---------------------------------------------------------------------------
# 2. _on_event — else-branch re-anchor to skip-to-latest on ttl_expired
# ---------------------------------------------------------------------------

@pytest.fixture
def _in_tmp(tmp_path, monkeypatch):
    # cursor files are written to relative paths (cwd); isolate per-test.
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _make_handler():
    h = ne.NewEventEvent()
    h.runtime = SimpleNamespace(
        subscription=SimpleNamespace(
            properties={"base_url": "https://api.example.com"},
            endpoint="https://api.example.com/sub/46f8ec4a",
        )
    )
    return h


def _latest_event():
    return {
        "event_id": "evt_latest",
        "sequence_number": 100,
        "event_type": "composer.text",
        "payload": {"message": "hi"},
        "metadata": {},
    }


def test_ttl_expired_reanchors_to_oldest_retained_fifo(_in_tmp):
    sub_id = "46f8ec4a"
    ne._write_cursor(sub_id, "evt_aged_out")  # stale cursor pinned to aged-out evt
    assert ne._cursor_path(sub_id).is_file()

    calls = []

    def fake_read(*, after_event_id, order, **kw):
        calls.append((after_event_id, order))
        if after_event_id == "evt_aged_out":
            raise HttpError(404, _ttl_body())   # catch-up hits ttl_expired
        # re-anchor pull (after=None, asc): server returns oldest-retained first.
        return [
            {"event_id": "evt_oldest", "sequence_number": 10, "event_type": "composer.text", "payload": {"message": "old"}, "metadata": {}},
            {"event_id": "evt_newer", "sequence_number": 11, "event_type": "composer.text", "payload": {"message": "new"}, "metadata": {}},
        ]

    with patch.object(ne, "parse_subscription_id", return_value=sub_id), \
         patch.object(ne, "resolve_credentials_from_cache", return_value=("cid", "csec")), \
         patch.object(ne, "read_events", side_effect=fake_read):
        out = _make_handler()._on_event(
            request=None,
            parameters={},
            payload={"topic_id": "top_x", "event_id": "evt_hint", "sequence_number": 9999},
        )

    # FIFO: the OLDEST retained event is processed (no skip-to-latest gap).
    assert out.variables["event_id"] == "evt_oldest"
    # catch-up tried the stale cursor (asc), then re-anchored from oldest (after=None, asc).
    assert calls == [("evt_aged_out", "asc"), (None, "asc")]
    # cursor advanced to the processed (oldest) event; stale id gone.
    data = json.loads(ne._cursor_path(sub_id).read_text())
    assert data["last_processed_event_id"] == "evt_oldest"


def test_non_ttl_http_error_propagates_and_keeps_cursor(_in_tmp):
    sub_id = "46f8ec4a"
    ne._write_cursor(sub_id, "evt_aged_out")

    def fake_read(*, after_event_id, **kw):
        raise HttpError(500, json.dumps({"detail": "boom"}))

    with patch.object(ne, "parse_subscription_id", return_value=sub_id), \
         patch.object(ne, "resolve_credentials_from_cache", return_value=("cid", "csec")), \
         patch.object(ne, "read_events", side_effect=fake_read):
        with pytest.raises(HttpError) as ei:
            _make_handler()._on_event(
                request=None,
                parameters={},
                payload={"topic_id": "top_x", "event_id": "evt_hint", "sequence_number": 9999},
            )
    assert ei.value.status == 500
    # cursor untouched on a non-ttl failure (no spurious re-anchor)
    data = json.loads(ne._cursor_path(sub_id).read_text())
    assert data["last_processed_event_id"] == "evt_aged_out"


def test_first_hint_uses_skip_to_latest(_in_tmp):
    # cursor=None path also routes through skip-to-latest (after=None, desc, 1).
    calls = []

    def fake_read(*, after_event_id, limit, order, **kw):
        calls.append((after_event_id, limit, order))
        return [_latest_event()]

    with patch.object(ne, "parse_subscription_id", return_value="newsub"), \
         patch.object(ne, "resolve_credentials_from_cache", return_value=("cid", "csec")), \
         patch.object(ne, "read_events", side_effect=fake_read):
        out = _make_handler()._on_event(
            request=None,
            parameters={},
            payload={"topic_id": "top_x", "event_id": "evt_hint", "sequence_number": 9999},
        )
    assert out.variables["event_id"] == "evt_latest"
    assert calls == [(None, 1, "desc")]


# ---------------------------------------------------------------------------
# 3. _prune_stale_cursors — bounded on-disk state (v1.0.9)
# ---------------------------------------------------------------------------

def _age(sub_id: str, seconds_ago: float) -> None:
    t = time.time() - seconds_ago
    os.utime(ne._cursor_path(sub_id), (t, t))


def test_prune_removes_only_stale_dead_subscription_cursors(_in_tmp):
    ne._write_cursor("live", "evt_live")
    ne._write_cursor("dead", "evt_dead")
    ne._write_cursor("recent_other", "evt_other")
    _age("dead", ne.CURSOR_STALE_SECONDS + 100)   # past retention → prunable
    _age("live", ne.CURSOR_STALE_SECONDS + 100)   # old but is the current sub

    ne._prune_stale_cursors("live")

    assert not ne._cursor_path("dead").exists()        # stale dead sub → pruned
    assert ne._cursor_path("live").exists()            # current sub kept (by name)
    assert ne._cursor_path("recent_other").exists()    # recent → kept


def test_prune_keeps_recent_cursors(_in_tmp):
    ne._write_cursor("a", "evt_a")
    ne._write_cursor("b", "evt_b")
    _age("b", ne.CURSOR_STALE_SECONDS - 3600)  # just under threshold
    ne._prune_stale_cursors("a")
    assert ne._cursor_path("a").exists()
    assert ne._cursor_path("b").exists()


def test_prune_is_noop_with_no_cursor_files(_in_tmp):
    ne._prune_stale_cursors("nobody")  # must not raise
    assert not ne._cursor_path("nobody").exists()


def test_prune_does_not_touch_unrelated_files(_in_tmp):
    ne._write_cursor("dead", "evt_dead")
    _age("dead", ne.CURSOR_STALE_SECONDS + 100)
    other = Path(".agentrux_activated.json")
    other.write_text("{}")  # credential cache must survive cursor prune
    ne._prune_stale_cursors("live")
    assert not ne._cursor_path("dead").exists()
    assert other.exists()
