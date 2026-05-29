"""SSE pull worker for NAT-bound Dify deployments.

When the user picks `delivery_mode=sse`, the plugin can't rely on
AgenTrux POSTing webhook hints (the Dify endpoint is not reachable from
the public internet). Instead, this worker holds an outbound SSE
connection to AgenTrux per granted-read topic and POSTs hint payloads
back to the plugin's own subscription endpoint as events arrive.

The loopback POST hits Dify locally (same docker network), then the
normal `_dispatch_event` → `_on_event` chain runs unchanged. The worker
is essentially impersonating AgenTrux's webhook_dispatcher from inside
the NAT.

Why a thread per topic, not one big multiplexer:
  AgenTrux's /topics/{id}/events/stream is single-topic. To watch N
  topics we need N connections. A thread-per-topic is simpler than a
  selector loop and the demo never has more than a few topics.

Why urllib, not httpx:
  dify_plugin runs under gevent monkey-patching; httpx hangs there.
  urllib's blocking sockets are gevent-patched safely.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import tempfile
import threading
import time
import urllib.error
import urllib.request

from provider.agentrux_api import (
    fetch_granted_topics,
    get_token,
    rewrite_endpoint_for_inner_api,
    validate_activation,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State file (one per subscription, lets us resume after plugin restart)
# ---------------------------------------------------------------------------

_STATE_PREFIX = ".agentrux_sse_"


def _state_path(endpoint: str) -> pathlib.Path:
    h = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
    return pathlib.Path(f"{_STATE_PREFIX}{h}.json")


def _write_state(endpoint: str, base_url: str, activation_code: str) -> None:
    p = _state_path(endpoint)
    payload = {
        "endpoint": endpoint,
        "base_url": base_url,
        "activation_code": activation_code,
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8",
        dir=str(p.parent) or ".",
        prefix=_STATE_PREFIX, suffix=".tmp", delete=False,
    )
    try:
        json.dump(payload, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    finally:
        tmp.close()
    os.chmod(tmp.name, 0o600)
    os.replace(tmp.name, p)


def _delete_state(endpoint: str) -> None:
    try:
        _state_path(endpoint).unlink()
    except FileNotFoundError:
        pass


def _load_all_states() -> list[dict]:
    out: list[dict] = []
    for f in pathlib.Path(".").glob(f"{_STATE_PREFIX}*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("sse: ignoring corrupt state file %s: %s", f, e)
    return out


# ---------------------------------------------------------------------------
# Worker registry
# ---------------------------------------------------------------------------

_workers: dict[str, "SubscriptionWorker"] = {}
_workers_lock = threading.Lock()


class _TopicStream(threading.Thread):
    """Holds one SSE connection to AgenTrux for a single topic."""

    def __init__(
        self, base_url: str, client_id: str, client_secret: str,
        topic_id: str, endpoint: str, stop_event: threading.Event,
    ):
        super().__init__(daemon=True, name=f"agentrux-sse-{topic_id[:8]}")
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.topic_id = topic_id
        self.endpoint = endpoint
        self.stop_event = stop_event
        self.last_event_id: str | None = None

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._stream_once()
                # Clean disconnect (server closed) — reconnect immediately.
                backoff = 1.0
            except Exception as e:
                logger.warning(
                    "[sse %s] connection error: %s; retrying in %.1fs",
                    self.topic_id, e, backoff,
                )
            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    def _stream_once(self) -> None:
        token = get_token(self.base_url, self.client_id, self.client_secret)
        # pipe_router enforces the `top_` prefix; JWT scope claims carry a
        # bare UUID, so prepend the prefix here.
        top_id = self.topic_id if self.topic_id.startswith("top_") else f"top_{self.topic_id}"
        url = f"{self.base_url}/topics/{top_id}/events/stream"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        }
        if self.last_event_id is not None:
            headers["Last-Event-ID"] = str(self.last_event_id)

        req = urllib.request.Request(url, headers=headers)
        # 60s socket timeout: AgenTrux SSE keepalive is 30s, so a healthy
        # connection always sees data within 60s. No data for 60s = dead
        # connection — bubble out and reconnect.
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RuntimeError(f"SSE handshake status={resp.status}")
            logger.info("[sse %s] connected", self.topic_id)
            # SSE frame is multi-line terminated by blank line。 Backend (Phase 2.5b SSOT) sends:
            #   event: hint\nid: evt_<uuid>\ndata: {"seq": N}\n\n
            #   event: heartbeat\ndata: {}\n\n
            #   event: ready\ndata: {}\n\n
            # 1 frame = 1 dict (event/id/data ごと)、 blank line で flush
            current_frame: dict[str, str] = {}
            for raw_line in resp:
                if self.stop_event.is_set():
                    return
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    # frame boundary → flush
                    if current_frame:
                        self._handle_frame(current_frame)
                        current_frame = {}
                    continue
                if line.startswith(":"):
                    continue  # keepalive comment
                if ":" not in line:
                    continue
                k, _, v = line.partition(":")
                current_frame[k.strip()] = v.lstrip()

    def _handle_frame(self, frame: dict[str, str]) -> None:
        """SSE 1 frame を処理。 hint 以外 (ready/heartbeat) は捨てる。"""
        if frame.get("event") != "hint":
            return
        evt_id = frame.get("id", "").strip()
        if evt_id:
            self.last_event_id = evt_id
        # data は {"seq": N} 形式 (Phase 2.5b SSOT)、 旧 latest_sequence_no は廃止
        seq: int | None = None
        try:
            payload = json.loads(frame.get("data", "{}"))
            if isinstance(payload, dict) and "seq" in payload:
                seq = int(payload["seq"])
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        if not evt_id or seq is None:
            return
        self._loopback_post(evt_id, seq)

    def _loopback_post(self, event_id: str, sequence_number: int) -> None:
        """plugin endpoint に hint 内容を loopback POST (M9 webhook 経路を借りる)。

        新 spec field 名:
          - event_id (旧 webhook には無かった、 SSE で新規追加)
          - sequence_number (旧 latest_sequence_no 廃止)
        downstream `_dispatch_event` も新 field 名で読む。
        """
        target_url = rewrite_endpoint_for_inner_api(self.endpoint)

        body = json.dumps({
            "topic_id": self.topic_id,
            "event_id": event_id,
            "sequence_number": sequence_number,
            "timestamp": int(time.time()),
            "delivery": "sse",
        }).encode("utf-8")
        req = urllib.request.Request(
            target_url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status >= 300:
                    logger.warning(
                        "[sse %s] loopback POST status=%s",
                        self.topic_id, resp.status,
                    )
        except urllib.error.URLError as e:
            logger.warning("[sse %s] loopback POST failed: %s", self.topic_id, e)


class SubscriptionWorker:
    """Manages all per-topic SSE threads for one Dify subscription."""

    def __init__(self, endpoint: str, base_url: str, activation_code: str):
        self.endpoint = endpoint
        self.base_url = base_url.rstrip("/")
        self.activation_code = activation_code
        self.stop_event = threading.Event()
        self.threads: list[_TopicStream] = []

    def start(self) -> None:
        try:
            cid, secret = validate_activation(self.base_url, self.activation_code)
        except Exception as e:
            logger.error("[sse] activation failed for %s: %s", self.endpoint, e)
            return
        topics = fetch_granted_topics(self.base_url, cid, secret)
        read_topics = sorted({t["topic_id"] for t in topics if t.get("action") == "read"})
        if not read_topics:
            logger.warning("[sse] no read-granted topics for endpoint=%s", self.endpoint)
            return
        logger.info(
            "[sse] starting %d topic streams for endpoint=%s topics=%s",
            len(read_topics), self.endpoint, read_topics,
        )
        for tid in read_topics:
            t = _TopicStream(
                self.base_url, cid, secret, tid, self.endpoint, self.stop_event,
            )
            t.start()
            self.threads.append(t)

    def stop(self) -> None:
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start(endpoint: str, base_url: str, activation_code: str,
          *, persist: bool = True) -> None:
    """Start (or restart) the SSE worker for one subscription endpoint."""
    if persist:
        try:
            _write_state(endpoint, base_url, activation_code)
        except Exception as e:
            logger.warning("[sse] state persist failed: %s", e)

    with _workers_lock:
        existing = _workers.get(endpoint)
        if existing:
            existing.stop()
        w = SubscriptionWorker(endpoint, base_url, activation_code)
        _workers[endpoint] = w
    w.start()


def stop(endpoint: str) -> None:
    """Stop the SSE worker and forget the persisted state."""
    with _workers_lock:
        w = _workers.pop(endpoint, None)
    if w:
        w.stop()
    _delete_state(endpoint)


def resume_persisted() -> int:
    """Restart workers from disk state (call once on plugin startup)."""
    states = _load_all_states()
    for s in states:
        try:
            start(
                s["endpoint"], s["base_url"], s["activation_code"],
                persist=False,  # already on disk
            )
        except Exception as e:
            logger.warning("[sse] resume failed for %s: %s", s.get("endpoint"), e)
    return len(states)
