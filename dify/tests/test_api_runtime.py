"""Unit tests for the agentrux_api runtime helpers."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure dify_plugin stubs exist (re-use the install from oauth tests).
sys.path.insert(0, str(Path(__file__).parent))
import test_oauth_provider  # noqa: F401  (installs stubs as side-effect)

BUILD_ROOT = Path("/tmp/agentrux-dify-build")
sys.path.insert(0, str(BUILD_ROOT))

from provider import agentrux_api  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    agentrux_api._cc_token_cache.clear()
    yield
    agentrux_api._cc_token_cache.clear()


# ---------------------------------------------------------------------------
# resolve_access_token
# ---------------------------------------------------------------------------

def test_resolve_returns_oauth_token_directly():
    base_url, token = agentrux_api.resolve_access_token(
        {"base_url": "https://api.agentrux.com", "access_token": "ey.oauth"}
    )
    assert base_url == "https://api.agentrux.com"
    assert token == "ey.oauth"


def test_resolve_falls_back_to_client_credentials():
    fake = MagicMock()
    fake.json.return_value = {"access_token": "ey.cc", "expires_in": 3600}
    with patch("provider.agentrux_api.httpx.post", return_value=fake) as m:
        base_url, token = agentrux_api.resolve_access_token(
            {
                "base_url": "https://api.agentrux.com",
                "client_id": "script_abc",
                "client_secret": "s",
            }
        )
    assert token == "ey.cc"
    kwargs = m.call_args.kwargs
    assert kwargs["data"]["grant_type"] == "client_credentials"


def test_resolve_caches_client_credentials_token():
    fake = MagicMock()
    fake.json.return_value = {"access_token": "ey.cc", "expires_in": 3600}
    with patch("provider.agentrux_api.httpx.post", return_value=fake) as m:
        creds = {
            "base_url": "https://api.agentrux.com",
            "client_id": "script_abc",
            "client_secret": "s",
        }
        agentrux_api.resolve_access_token(creds)
        agentrux_api.resolve_access_token(creds)
    assert m.call_count == 1, "second call should hit cache"


def test_resolve_rejects_http_base_url():
    with pytest.raises(ValueError, match="HTTPS"):
        agentrux_api.resolve_access_token(
            {"base_url": "http://api.agentrux.com", "access_token": "ey.x"}
        )


def test_resolve_raises_when_no_credentials():
    with pytest.raises(ValueError, match="No credentials"):
        agentrux_api.resolve_access_token(
            {"base_url": "https://api.agentrux.com"}
        )


# ---------------------------------------------------------------------------
# JWT scope decode -> topic options
# ---------------------------------------------------------------------------

def _jwt(scope):
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"scope": scope}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


def test_build_topic_options_filters_by_action():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt(
            ["topic:t1:read", "topic:t1:write", "topic:t2:read", "other:x"]
        ),
    }
    options = agentrux_api.build_topic_options(creds, {"write"})
    assert options == [{"label": "t1 (write)", "value": "t1"}]


def test_build_topic_options_handles_string_scope():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt("topic:t1:read topic:t2:write"),
    }
    options = agentrux_api.build_topic_options(creds, {"read", "write"})
    values = sorted(o["value"] for o in options)
    assert values == ["t1", "t2"]


def test_build_topic_options_dedupes():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt(["topic:t1:read", "topic:t1:read"]),
    }
    assert len(agentrux_api.build_topic_options(creds, {"read"})) == 1


def test_build_topic_options_swallows_errors():
    # No credentials -> resolve throws; build_topic_options returns []
    assert agentrux_api.build_topic_options({}, {"read"}) == []


# ---------------------------------------------------------------------------
# publish_event / read_events
# ---------------------------------------------------------------------------

def test_publish_event_sends_authorized_post():
    fake = MagicMock()
    fake.json.return_value = {"event_id": "e1", "sequence_no": 7}
    with patch("provider.agentrux_api.httpx.post", return_value=fake) as m:
        out = agentrux_api.publish_event(
            creds={
                "base_url": "https://api.agentrux.com",
                "access_token": "ey.x",
            },
            topic_id="t1",
            event_type="evt",
            payload={"x": 1},
            correlation_id="c1",
        )
    assert out["event_id"] == "e1"
    args, kwargs = m.call_args
    assert args[0] == "https://api.agentrux.com/topics/t1/events"
    assert kwargs["headers"]["Authorization"] == "Bearer ey.x"
    assert kwargs["json"]["correlation_id"] == "c1"


def test_read_events_passes_filters():
    fake = MagicMock()
    fake.json.return_value = {"items": [{"id": "e1"}]}
    with patch("provider.agentrux_api.httpx.get", return_value=fake) as m:
        events = agentrux_api.read_events(
            creds={
                "base_url": "https://api.agentrux.com",
                "access_token": "ey.x",
            },
            topic_id="t1",
            limit=5,
            event_type="evt",
        )
    assert events == [{"id": "e1"}]
    kwargs = m.call_args.kwargs
    assert kwargs["params"]["limit"] == "5"
    assert kwargs["params"]["type"] == "evt"
