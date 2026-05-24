"""SSE hint stream consumer.

The server side (`pipe_router.py` + `sse_hint_stream_adapter.py`) emits
four frame types and NEVER ships full event payloads:

  event: ready                  # connection established
  data: {"topic_id":..., "resume_from": "evt_<uuid>"|null, "heartbeat_seconds":N}

  event: hint                   # new event arrived; full body via pull
  id:    evt_<uuid>
  data:  {"topic_id":..., "event_id":"evt_<uuid>", "seq":123, "ts":"ISO"}

  event: resync_required        # cursor invalid / overflow / etc.
  data:  {"reason":..., "request_id":"req_<hex>", "next_action":"pull_resync",
          "details": {"resume_via": "...", "max_catchup": N?}}

  event: close                  # server says don't reconnect (banned)
  data:  {"reason": "banned"}

Plus heartbeat comment lines `: heartbeat` we ignore.

This module owns the SSE socket and dispatches each frame to user-
supplied callbacks. It does NOT itself perform pulls — that's the
HybridConsumer's job, which composes this with a PullClient.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.errors import (
    AccessDeniedError,
    ConnectionBannedError,
    ResyncRequiredError,
    SDKError,
)
from agentrux.sdk.reconnect import ExponentialBackoff
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.sse")


@dataclass(frozen=True)
class HintFrame:
    event_id: str          # "evt_<uuid>"
    sequence_number: int
    topic_id: str          # "top_<uuid>"
    timestamp: str         # ISO


@dataclass(frozen=True)
class ReadyFrame:
    topic_id: str
    resume_from: str | None   # "evt_<uuid>" or None
    heartbeat_seconds: int


@dataclass(frozen=True)
class ResyncFrame:
    reason: str
    request_id: str
    resume_via: str | None
    max_catchup: int | None


OnReady = Callable[[ReadyFrame], Awaitable[None]]
OnHint = Callable[[HintFrame], Awaitable[None]]
OnResync = Callable[[ResyncFrame], Awaitable[None]]
OnClose = Callable[[str], Awaitable[None]]


class SSEClient:
    """SSE consumer that parses hint frames and dispatches via callbacks.

    Reconnection policy:
      - On clean server-close: reconnect (uses ExponentialBackoff).
      - On `event: close` (banned): raise ConnectionBannedError; do not reconnect.
      - On `event: resync_required`: dispatch callback, then optionally
        continue (caller chooses by calling `clear_resume()` before the
        next run).
      - On HTTP 403: raise AccessDeniedError; do not reconnect.

    The same SSEClient can be re-`run()` after disconnect; each call to
    `run()` starts fresh from `last_event_id` (initialized via
    constructor or `set_last_event_id`).
    """

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        last_event_id: str | None = None,
        reconnect_strategy: ExponentialBackoff | None = None,
        on_ready: OnReady | None = None,
        on_hint: OnHint | None = None,
        on_resync_required: OnResync | None = None,
        on_close: OnClose | None = None,
        on_error: Callable[[Exception], Awaitable[None]] | None = None,
    ) -> None:
        if not topic_id.startswith("top_"):
            raise ValueError(f"topic_id must start with 'top_', got {topic_id!r}")
        if last_event_id is not None and not last_event_id.startswith("evt_"):
            raise ValueError(
                f"last_event_id must start with 'evt_', got {last_event_id!r}"
            )
        self._api = api_client
        self._topic_id = topic_id
        self._last_event_id = last_event_id
        self._reconnect = reconnect_strategy or ExponentialBackoff()
        self._on_ready = on_ready
        self._on_hint = on_hint
        self._on_resync_required = on_resync_required
        self._on_close = on_close
        self._on_error = on_error
        self._stop_event = asyncio.Event()
        self._stats = SDKStats(current_mode="sse")

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    def set_last_event_id(self, event_id: str | None) -> None:
        if event_id is not None and not event_id.startswith("evt_"):
            raise ValueError(f"event_id must start with 'evt_', got {event_id!r}")
        self._last_event_id = event_id

    async def stop(self) -> None:
        self._stop_event.set()
        self._stats.current_mode = "disconnected"

    async def run(self) -> None:
        """Run until stopped, banned, or max-reconnect-attempts exhausted.

        Raises ConnectionBannedError on `event: close reason=banned`.
        Other errors are surfaced via on_error (if provided) plus
        normal reconnection; if reconnection is exhausted, the most
        recent error is re-raised.
        """
        attempt = 0
        last_error: Exception | None = None
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except ConnectionBannedError:
                # Permanent — bubble up.
                raise
            except ResyncRequiredError:
                # Cursor invalid → reconnecting would just hit the same
                # error. Bubble up so the consumer can reset checkpoint
                # before re-subscribing.
                raise
            except (asyncio.CancelledError, KeyboardInterrupt):
                return
            except Exception as exc:  # noqa: BLE001 - SSE socket can fail many ways
                last_error = exc
                self._stats.errors += 1
                if self._on_error is not None:
                    try:
                        await self._on_error(exc)
                    except Exception:  # noqa: BLE001 - user callback isolation
                        logger.exception("on_error callback raised")
                logger.warning("SSE error on topic=%s: %s", self._topic_id, exc)
                if not self._reconnect.should_retry(attempt):
                    raise
                delay_ms = self._reconnect.next_delay(attempt)
                attempt += 1
                self._stats.reconnections += 1
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay_ms / 1000
                    )
                    return  # stop_event fired during backoff
                except asyncio.TimeoutError:
                    continue

            # Clean server-close: reconnect (matching SSE semantics).
            if self._stop_event.is_set():
                return
            # Honour the strategy's `should_retry` here too — a strategy
            # that says "no retries" should make a clean close terminal
            # (used by tests and by short-lived consumers that want a
            # one-shot drain).
            if not self._reconnect.should_retry(attempt):
                return
            attempt = 0  # reset on graceful round-trip
        _ = last_error  # last_error retained for debugging if reraised above

    async def _run_once(self) -> None:
        tm = self._api.token_manager
        if tm is not None:
            await tm.ensure_valid()
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        if tm is not None:
            headers["Authorization"] = f"Bearer {tm.access_token}"
        if self._last_event_id is not None:
            headers["Last-Event-ID"] = self._last_event_id

        # Share the AgenTruxAPIClient's httpx instance so MockTransport
        # in tests (and any per-process http settings the caller configured)
        # apply to the SSE stream as well. Constructing a fresh
        # httpx.AsyncClient here would bypass both.
        url = f"/topics/{self._topic_id}/events/stream"
        async with self._api._http.stream("GET", url, headers=headers) as resp:  # noqa: SLF001
            if resp.status_code == 403:
                request_id = resp.headers.get("X-Request-Id")
                raise AccessDeniedError(
                    f"SSE 403 on topic={self._topic_id}"
                    + (f" (request_id={request_id})" if request_id else ""),
                    status_code=403,
                    error="access_denied",
                )
            if resp.status_code != 200:
                request_id = resp.headers.get("X-Request-Id")
                raise SDKError(
                    f"SSE HTTP {resp.status_code} on topic={self._topic_id}"
                    + (f" (request_id={request_id})" if request_id else "")
                )
            await self._consume_stream(resp)

    async def _consume_stream(self, resp: httpx.Response) -> None:
        """Parse SSE frames out of the response stream."""
        event_name: str | None = None
        data_lines: list[str] = []
        frame_id: str | None = None

        async for line in resp.aiter_lines():
            if self._stop_event.is_set():
                return
            if line == "":
                # Frame boundary — process accumulated lines.
                if event_name is not None or data_lines:
                    await self._dispatch_frame(event_name, frame_id, data_lines)
                event_name = None
                data_lines = []
                frame_id = None
                continue
            if line.startswith(":"):
                # Comment / heartbeat — ignore.
                continue
            if line.startswith("event:"):
                event_name = line[len("event:") :].strip()
                continue
            if line.startswith("id:"):
                frame_id = line[len("id:") :].strip()
                # SSE spec: the id is also the value the client sends in
                # the next Last-Event-ID header. Server emits evt_<uuid>
                # for hint frames; we update on dispatch.
                continue
            if line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
                continue
            # Unknown line; skip per SSE spec.

    async def _dispatch_frame(
        self,
        event_name: str | None,
        frame_id: str | None,
        data_lines: list[str],
    ) -> None:
        if not data_lines:
            return
        payload_str = "\n".join(data_lines)
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning(
                "SSE: malformed data on event=%r: %s",
                event_name,
                payload_str[:200],
            )
            return
        if not isinstance(payload, dict):
            logger.warning("SSE: data must be JSON object, got %r", type(payload))
            return

        if event_name == "hint":
            await self._on_hint_dispatch(frame_id, payload)
        elif event_name == "ready":
            await self._on_ready_dispatch(payload)
        elif event_name == "resync_required":
            await self._on_resync_dispatch(payload)
        elif event_name == "close":
            await self._on_close_dispatch(payload)
        else:
            # message / unnamed events are not currently emitted; ignore.
            logger.debug("SSE: ignoring frame event=%r", event_name)

    async def _on_ready_dispatch(self, payload: dict) -> None:
        try:
            frame = ReadyFrame(
                topic_id=str(payload["topic_id"]),
                resume_from=payload.get("resume_from"),
                heartbeat_seconds=int(payload.get("heartbeat_seconds", 30)),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("SSE ready frame malformed: %s; data=%r", exc, payload)
            return
        if self._on_ready is not None:
            try:
                await self._on_ready(frame)
            except Exception:  # noqa: BLE001 - user callback isolation
                logger.exception("on_ready callback raised")

    async def _on_hint_dispatch(self, frame_id: str | None, payload: dict) -> None:
        try:
            event_id = str(payload["event_id"])
            if not event_id.startswith("evt_"):
                logger.warning("SSE hint event_id without prefix: %r", event_id)
                return
            frame = HintFrame(
                event_id=event_id,
                sequence_number=int(payload["seq"]),
                topic_id=str(payload["topic_id"]),
                timestamp=str(payload["ts"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            logger.warning("SSE hint frame malformed: %s; data=%r", exc, payload)
            return
        # Advance the Last-Event-ID we'll send on the next reconnect.
        self._last_event_id = frame.event_id
        self._stats.messages_received += 1
        if self._on_hint is not None:
            try:
                await self._on_hint(frame)
            except Exception:  # noqa: BLE001 - user callback isolation
                logger.exception("on_hint callback raised")

    async def _on_resync_dispatch(self, payload: dict) -> None:
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        try:
            frame = ResyncFrame(
                reason=str(payload.get("reason", "unknown")),
                request_id=str(payload.get("request_id", "")),
                resume_via=details.get("resume_via"),
                max_catchup=(
                    int(details["max_catchup"])
                    if "max_catchup" in details
                    else None
                ),
            )
        except (ValueError, TypeError) as exc:
            logger.warning("SSE resync frame malformed: %s; data=%r", exc, payload)
            return
        if self._on_resync_required is not None:
            # User callback is allowed to raise — and SHOULD raise (or
            # otherwise propagate) so the consumer above this layer
            # learns that the cursor is invalid and a checkpoint reset
            # is needed. We do NOT catch ResyncRequiredError (or any
            # other exception) here; isolating callback exceptions for
            # this specific frame would silently strand the consumer at
            # a dead cursor. See Codex impl review #4.
            await self._on_resync_required(frame)
        else:
            # No callback → surface as exception so the caller can react.
            raise ResyncRequiredError(
                f"SSE resync_required (reason={frame.reason})",
                reason=frame.reason,
                request_id=frame.request_id,
                resume_via=frame.resume_via,
                max_catchup=frame.max_catchup,
            )

    async def _on_close_dispatch(self, payload: dict) -> None:
        reason = str(payload.get("reason", "unknown"))
        if self._on_close is not None:
            try:
                await self._on_close(reason)
            except Exception:  # noqa: BLE001 - user callback isolation
                logger.exception("on_close callback raised")
        if reason == "banned":
            raise ConnectionBannedError(
                f"Server closed SSE: account banned (topic={self._topic_id})"
            )
        # Other reasons → let the reconnect loop in run() try again.

    @property
    def stats(self) -> SDKStats:
        return self._stats
