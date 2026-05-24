"""HybridConsumer - pull-based consumption with SSE hint as a low-latency waker.

Design (v0.3):
- Pull is the canonical source. PullClient owns the cursor and emits
  events through the pipeline (deduplicator + reorder + flow).
- SSE is hint-only: a `hint` frame says "new event arrived on this
  topic"; we call `pull_client.wake()` to skip the current poll sleep
  and fetch immediately. The actual event body comes via Pull.
- This avoids re-implementing event ordering / dedup / gap detection
  on the SSE side, which would have to mirror Pull's pipeline anyway.

Behavior on SSE frames:
  ready             → log; record resume_from (for diagnostics).
  hint              → pull_client.wake().
  resync_required   → user-supplied on_resync_required callback if any,
                      else raise ResyncRequiredError. The application
                      decides whether to reset checkpoints + re-subscribe.
  close (banned)    → propagate ConnectionBannedError.

If SSE is permanently unavailable (e.g. NAT or firewall), the hybrid
still works via Pull alone — it just runs at the configured polling
interval without low-latency wake-ups.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Awaitable, Callable

from agentrux.sdk.client import AgenTruxAPIClient
from agentrux.sdk.envelope import MessageEnvelope
from agentrux.sdk.errors import ConnectionBannedError, ResyncRequiredError
from agentrux.sdk.pipeline import MessagePipeline
from agentrux.sdk.pull_client import PullClient
from agentrux.sdk.sse_client import HintFrame, ReadyFrame, ResyncFrame, SSEClient
from agentrux.sdk.stats import SDKStats

logger = logging.getLogger("agentrux.sdk.hybrid")


OnResyncRequired = Callable[[ResyncFrame], Awaitable[None]]


class HybridConsumer:
    """Pull-driven consumer with SSE hint waker."""

    def __init__(
        self,
        api_client: AgenTruxAPIClient,
        topic_id: str,
        *,
        pipeline: MessagePipeline | None = None,
        start_after_event_id: str | None = None,
        poll_interval_ms: int = 1000,
        min_interval_ms: int = 100,
        max_interval_ms: int = 30_000,
        batch_size: int = 100,
        sse_enabled: bool = True,
        on_resync_required: OnResyncRequired | None = None,
    ) -> None:
        self._api = api_client
        self._topic_id = topic_id
        self._pipeline = pipeline or MessagePipeline()
        self._pipeline.set_topic_id(topic_id)
        self._on_resync_required = on_resync_required
        self._sse_enabled = sse_enabled

        self._pull = PullClient(
            api_client=api_client,
            topic_id=topic_id,
            poll_interval_ms=poll_interval_ms,
            min_interval_ms=min_interval_ms,
            max_interval_ms=max_interval_ms,
            batch_size=batch_size,
            pipeline=self._pipeline,
            start_after_event_id=start_after_event_id,
        )
        self._sse: SSEClient | None = None
        if sse_enabled:
            self._sse = SSEClient(
                api_client=api_client,
                topic_id=topic_id,
                last_event_id=start_after_event_id,
                on_ready=self._on_ready,
                on_hint=self._on_hint,
                on_resync_required=self._on_resync,
                on_close=self._on_close,
            )

        self._sse_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._mode: str = "hybrid" if sse_enabled else "pull"
        self._banned = False
        # When the SSE side decides it cannot continue (resync_required
        # propagated through, or a user on_resync_required callback
        # re-raised), we stash the exception here and the iterator picks
        # it up on the next yield boundary. Pull is told to stop so the
        # iterator wakes promptly.
        self._fatal_sse_error: BaseException | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def stats(self) -> SDKStats:
        return self._pull.stats

    # --- SSE callbacks ------------------------------------------------------

    async def _on_ready(self, frame: ReadyFrame) -> None:
        logger.info(
            "SSE ready on topic=%s, resume_from=%s, heartbeat=%ss",
            frame.topic_id, frame.resume_from, frame.heartbeat_seconds,
        )

    async def _on_hint(self, frame: HintFrame) -> None:
        # The hint says: a new event with this seq exists. Don't try to
        # consume the (non-existent) body off the wire — just trigger a
        # poll. The pull cycle picks it up via list_events(after=cursor).
        logger.debug(
            "SSE hint topic=%s evt=%s seq=%d → wake pull",
            frame.topic_id, frame.event_id, frame.sequence_number,
        )
        self._pull.wake()

    async def _on_resync(self, frame: ResyncFrame) -> None:
        logger.warning(
            "SSE resync_required reason=%s request_id=%s resume_via=%s",
            frame.reason, frame.request_id, frame.resume_via,
        )
        # Notify the user's callback first (best-effort: a callback
        # exception is a hook-side bug and must not change the
        # consumer's must-stop semantics). The callback is for logging
        # / metrics / push notifications, not for deciding control flow.
        if self._on_resync_required is not None:
            try:
                await self._on_resync_required(frame)
            except Exception:  # noqa: BLE001 - hook isolation
                logger.exception("on_resync_required callback raised")
        # ALWAYS raise: resync_required means the cursor is unrecoverable
        # and the consumer must re-subscribe with fresh state. SSE.run()
        # propagates this; _run_sse stores it in _fatal_sse_error so the
        # iterator picks it up on the next yield boundary (impl review #4
        # / 2nd review iteration).
        raise ResyncRequiredError(
            f"resync_required (reason={frame.reason})",
            reason=frame.reason,
            request_id=frame.request_id,
            resume_via=frame.resume_via,
            max_catchup=frame.max_catchup,
        )

    async def _on_close(self, reason: str) -> None:
        if reason == "banned":
            self._banned = True
            logger.error(
                "SSE close reason=banned for topic=%s — stopping hybrid",
                self._topic_id,
            )
        else:
            logger.info("SSE close reason=%s; will reconnect", reason)

    # --- Public iterator API -----------------------------------------------

    async def __aiter__(self) -> AsyncIterator[MessageEnvelope]:
        if self._sse is not None and self._sse_task is None:
            self._sse_task = asyncio.create_task(self._run_sse())

        try:
            # Check for an SSE-side fatal error BEFORE entering the
            # pull loop — if it was set synchronously (e.g. by a test
            # or by the SSE task raising before pull yields anything)
            # we must surface immediately rather than wait for the
            # first pull yield (which may never come).
            if self._fatal_sse_error is not None:
                raise self._fatal_sse_error
            if self._banned:
                raise ConnectionBannedError(
                    f"Account banned, stopping consumer "
                    f"(topic={self._topic_id})"
                )
            async for msg in self._pull:
                if self._banned:
                    raise ConnectionBannedError(
                        f"Account banned, stopping consumer "
                        f"(topic={self._topic_id})"
                    )
                if self._fatal_sse_error is not None:
                    # Surface SSE-side fatal errors (resync_required and
                    # friends) at the next iteration boundary. Pull was
                    # already asked to stop so this is the last yield.
                    raise self._fatal_sse_error
                yield msg
        finally:
            await self.stop()

    async def _run_sse(self) -> None:
        assert self._sse is not None
        try:
            await self._sse.run()
        except ConnectionBannedError:
            self._banned = True
            await self._pull.stop()  # wake the pull iterator to break out
        except ResyncRequiredError as exc:
            # The cursor is invalid; user must reset their checkpoint
            # before re-subscribing. Capture for the iterator to raise.
            self._fatal_sse_error = exc
            await self._pull.stop()
        except Exception:  # noqa: BLE001 - SSE side errors don't kill Pull
            logger.exception("SSE background task crashed (pull continues)")

    async def stop(self) -> None:
        self._stop.set()
        await self._pull.stop()
        if self._sse is not None:
            await self._sse.stop()
        if self._sse_task is not None:
            try:
                await asyncio.wait_for(self._sse_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sse_task.cancel()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.exception("SSE task shutdown raised")
            finally:
                self._sse_task = None

    def __repr__(self) -> str:
        return (
            f"HybridConsumer(topic={self._topic_id!r}, mode={self._mode!r}, "
            f"sse_task={'alive' if self._sse_task else 'none'})"
        )


__all__ = ["HybridConsumer"]
