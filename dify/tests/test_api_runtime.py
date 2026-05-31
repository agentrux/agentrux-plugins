"""Unit tests for the agentrux_api runtime helpers."""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
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
    agentrux_api.ACTIVATED_CACHE.clear()
    yield
    agentrux_api._cc_token_cache.clear()
    agentrux_api.ACTIVATED_CACHE.clear()


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
# Activation Code path (baseline A): act_ -> crd_/aks_ -> client_credentials
# ---------------------------------------------------------------------------

def test_resolve_via_activation_code():
    fake = MagicMock()
    fake.json.return_value = {"access_token": "ey.cc", "expires_in": 3600}
    with patch(
        "provider.agentrux_api.validate_activation",
        return_value=("crd_x", "aks_y"),
    ) as mv, patch("provider.agentrux_api.httpx.post", return_value=fake) as mp:
        base_url, token = agentrux_api.resolve_access_token(
            {
                "base_url": "https://api.agentrux.com",
                "activation_code": "act_abc",
            }
        )
    assert token == "ey.cc"
    mv.assert_called_once_with("https://api.agentrux.com", "act_abc")
    # client_credentials uses the redeemed crd_/aks_
    assert mp.call_args.kwargs["data"]["client_id"] == "crd_x"
    assert mp.call_args.kwargs["data"]["grant_type"] == "client_credentials"


def test_validate_activation_idempotent_via_disk_cache(tmp_path):
    cache_file = tmp_path / ".agentrux_activated.json"
    with patch.object(agentrux_api, "_DISK_CACHE_FILE", cache_file), patch(
        "provider.agentrux_api.activate", return_value=("crd_x", "aks_y")
    ) as ma:
        first = agentrux_api.validate_activation(
            "https://api.agentrux.com", "act_abc"
        )
        # Drop the in-process fast path so the 2nd call must consult disk.
        agentrux_api.ACTIVATED_CACHE.clear()
        second = agentrux_api.validate_activation(
            "https://api.agentrux.com", "act_abc"
        )
    assert first == second == ("crd_x", "aks_y")
    assert ma.call_count == 1, "same AC must redeem once (disk-cache idempotent)"
    assert cache_file.exists()


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


def _offline_get():
    """Patch GET /topics to fail, forcing the JWT-scope fallback path."""
    return patch(
        "provider.agentrux_api.httpx.get",
        side_effect=httpx.HTTPError("offline"),
    )


def test_build_topic_options_primary_uses_get_topics_names():
    # GET /topics returns named items (server-sorted); build options preserve
    # order and filter by action, labelling with the human-readable name.
    fake = MagicMock()
    fake.status_code = 200
    fake.json.return_value = {
        "items": [
            {"topic_id": "top_a", "name": "alerts", "display_name": "Alerts",
             "actions": ["read"]},
            {"topic_id": "top_b", "name": "orders", "display_name": "Orders",
             "actions": ["read", "write"]},
        ]
    }
    creds = {"base_url": "https://api.agentrux.com", "access_token": "ey.x"}
    with patch("provider.agentrux_api.httpx.get", return_value=fake) as mg:
        options = agentrux_api.build_topic_options(creds, {"write"})
    # only orders is write-granted; label is the display_name, value is top_id
    assert options == [{"label": "Orders", "value": "top_b"}]
    assert mg.call_args.args[0] == "https://api.agentrux.com/topics"
    assert mg.call_args.kwargs["headers"]["Authorization"] == "Bearer ey.x"


def test_build_topic_options_malformed_200_falls_back_to_scope():
    # 200 but the body is not valid JSON / wrong shape -> must fall back to the
    # JWT-scope path instead of raising (Codex impl review Q4).
    bad = MagicMock()
    bad.status_code = 200
    bad.json.side_effect = ValueError("not json")
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt(["topic:t1:write"]),
    }
    with patch("provider.agentrux_api.httpx.get", return_value=bad):
        options = agentrux_api.build_topic_options(creds, {"write"})
    assert options == [{"label": "t1 (write)", "value": "t1"}]


def test_build_topic_options_fallback_filters_by_action():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt(
            ["topic:t1:read", "topic:t1:write", "topic:t2:read", "other:x"]
        ),
    }
    with _offline_get():
        options = agentrux_api.build_topic_options(creds, {"write"})
    assert options == [{"label": "t1 (write)", "value": "t1"}]


def test_build_topic_options_fallback_handles_string_scope():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt("topic:t1:read topic:t2:write"),
    }
    with _offline_get():
        options = agentrux_api.build_topic_options(creds, {"read", "write"})
    values = sorted(o["value"] for o in options)
    assert values == ["t1", "t2"]


def test_build_topic_options_fallback_dedupes():
    creds = {
        "base_url": "https://api.agentrux.com",
        "access_token": _jwt(["topic:t1:read", "topic:t1:read"]),
    }
    with _offline_get():
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
