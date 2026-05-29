"""New Event handler with per-subscription cursor (Phase 2.5a SSOT)。

Cursor semantics:
  - Stored on disk keyed by subscription_id (same pattern as AC cache).
  - Format: `{last_processed_event_id: "evt_<uuid>"}` (旧 last_processed_seq int は廃止)
  - On each hint, pull events `?after=<cursor evt_id>` (limit 50)。
  - Dispatch the OLDEST unprocessed event whose sequence_number <= hint's,
    advance cursor to that event's event_id。
  - Remaining unprocessed events wait for future hints。
  - If hint.event_id == cursor: duplicate/reorder — ignore。

新 field 名 (Phase 2 SSOT):
  - event.event_id / event.sequence_number / event.event_type / event.payload_object_id
  - 旧 sequence_no / type / object_id / download_url は廃止
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import time
from collections.abc import Mapping
from typing import Any

from werkzeug import Request
from dify_plugin.entities.trigger import Variables
from dify_plugin.errors.trigger import EventIgnoreError
from dify_plugin.interfaces.trigger import Event

from provider.agentrux_api import (
    HttpError,
    get_payload_download_url,
    is_ttl_expired_cursor,
    parse_subscription_id,
    read_events,
    resolve_credentials_from_cache,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cursor persistence (disk, per-subscription)
# ---------------------------------------------------------------------------
# Phase 2.5a SSOT: cursor は evt_id 文字列。 ファイル名 _v2_ suffix で旧 cache 無効化。

def _cursor_path(subscription_id: str) -> pathlib.Path:
    safe_id = "".join(c for c in subscription_id if c.isalnum() or c in "-_")[:64]
    return pathlib.Path(f".agentrux_cursor_v2_{safe_id}.json")


def _read_cursor(subscription_id: str) -> str | None:
    """Return last-committed event_id (e.g. 'evt_<uuid>'), or None if no cursor yet."""
    try:
        p = _cursor_path(subscription_id)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get("last_processed_event_id")
        return str(v) if v else None
    except Exception as e:
        logger.warning("cursor read failed for %s: %s", subscription_id, e)
        return None


def _write_cursor(subscription_id: str, event_id: str) -> None:
    try:
        p = _cursor_path(subscription_id)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8",
            dir=str(p.parent) or ".",
            prefix=f".agentrux_cursor_{subscription_id[:8]}.",
            suffix=".tmp", delete=False,
        )
        try:
            json.dump({"last_processed_event_id": str(event_id)}, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        finally:
            tmp.close()
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, p)
    except Exception as e:
        logger.warning("cursor write failed for %s: %s", subscription_id, e)


def _clear_cursor(subscription_id: str) -> None:
    """Drop a stale cursor so the next hint takes the cursor=None (skip-to-latest)
    path. Used when the server reports the pinned cursor as ttl_expired — the
    aged-out event id can never become valid again."""
    try:
        p = _cursor_path(subscription_id)
        if p.is_file():
            p.unlink()
    except Exception as e:
        logger.warning("cursor clear failed for %s: %s", subscription_id, e)


# Retire cursor files that have not been touched in longer than the server's max
# retention window. Whether such a subscription is dead or merely quiet, its
# pinned evt_id is past retention, so the next hint re-anchors via skip-to-latest
# (the same path as a ttl_expired cursor) — deleting it is loss-free and keeps
# on-disk state bounded (avoids per-subscription file accumulation). The current
# subscription's cursor is always preserved by exact-name match.
CURSOR_STALE_SECONDS = 30 * 24 * 3600  # 30d == server RETENTION_MAX (topic_retention.py)


def _prune_stale_cursors(keep_subscription_id: str) -> None:
    try:
        keep = _cursor_path(keep_subscription_id)
        cutoff = time.time() - CURSOR_STALE_SECONDS
        for p in keep.parent.glob(".agentrux_cursor_v2_*.json"):
            if p == keep:
                continue
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except FileNotFoundError:
                pass
    except Exception as e:
        logger.warning("cursor prune failed: %s", e)


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

CURSOR_PULL_LIMIT = 50


class NewEventEvent(Event):
    def _on_event(
        self, request: Request, parameters: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> Variables:
        topic_id = payload.get("topic_id", "")
        hint_event_id = payload.get("event_id", "")
        hint_seq = payload.get("sequence_number")

        runtime = getattr(self, "runtime", None)
        subscription = getattr(runtime, "subscription", None) if runtime else None
        props = subscription.properties if subscription else {}
        base_url = (props.get("base_url", "") or "").rstrip("/")
        subscription_id = parse_subscription_id(subscription.endpoint) if subscription else ""

        if not topic_id or not hint_event_id or not base_url:
            raise EventIgnoreError()

        # Credentials live in the on-disk cache populated when the subscription
        # was created (AC consumed at that point). We never re-redeem the AC
        # here — properties does not carry it (it is a secret-input).
        creds = resolve_credentials_from_cache(base_url)
        if creds is None:
            raise EventIgnoreError()
        client_id, client_secret = creds

        if subscription_id:
            _prune_stale_cursors(subscription_id)

        cursor = _read_cursor(subscription_id) if subscription_id else None

        # 重複 hint (cursor == hint event_id) は ignore。
        if cursor == hint_event_id:
            raise EventIgnoreError()

        # First-hint path (cursor 無し = 新規 subscription): 最新 1 件のみ desc 取得。
        # 既存 history の一斉流し込みを避け、 caller が想定する 「新着 1 件」 に寄せる。
        # (ttl_expired (cursor は在ったが aged-out) は別扱い: 下の except で oldest 再
        # anchor し retained を取りこぼさず FIFO で流す。)
        def _skip_to_latest() -> list[dict]:
            return read_events(
                base_url=base_url,
                client_id=client_id,
                client_secret=client_secret,
                topic_id=topic_id,
                after_event_id=None,
                limit=1,
                order="desc",
            )

        if cursor is None:
            events = _skip_to_latest()
        else:
            # 通常 path: cursor 以降を catch up。 cursor が TTL で aged-out した場合は
            # 404 ttl_expired が返る → stale cursor を破棄し、 取得可能な最古から
            # FIFO で catch up し直す (oldest 再 anchor)。
            try:
                events = read_events(
                    base_url=base_url,
                    client_id=client_id,
                    client_secret=client_secret,
                    topic_id=topic_id,
                    after_event_id=cursor,
                    limit=CURSOR_PULL_LIMIT,
                    order="asc",
                )
            except HttpError as e:
                if not is_ttl_expired_cursor(e):
                    raise
                # aged-out cursor → 取得可能な最古から asc で引き直す。 after=None + asc で
                # サーバは oldest retained から返すので、 oldest..head の retained event を
                # 取りこぼさず FIFO で流せる (hint は jump するので skip-to-latest にすると
                # 中間 event が抜ける。 openclaw reanchorExpiredCursor の oldest 再 anchor
                # を default 化した形)。 cursor は処理した event で前進する。
                logger.warning(
                    "cursor %s ttl-expired for sub %s; re-anchoring to oldest retained",
                    cursor, subscription_id,
                )
                if subscription_id:
                    _clear_cursor(subscription_id)
                events = read_events(
                    base_url=base_url,
                    client_id=client_id,
                    client_secret=client_secret,
                    topic_id=topic_id,
                    after_event_id=None,
                    limit=CURSOR_PULL_LIMIT,
                    order="asc",
                )

        # hint の sequence_number 以下のみ処理 (それより新しいものは次回 hint で扱う)。
        # hint_seq が無い (旧 webhook 互換 path) なら filter しない。
        try:
            seq_cap = int(hint_seq) if hint_seq is not None else None
        except (TypeError, ValueError):
            seq_cap = None
        unprocessed = sorted(
            (
                e for e in events
                if seq_cap is None or int(e.get("sequence_number", 0)) <= seq_cap
            ),
            key=lambda e: int(e.get("sequence_number", 0)),
        )
        if not unprocessed:
            raise EventIgnoreError()

        event_type_filter = parameters.get("event_type_filter", "")
        # Skip filtered events, advancing cursor as we go.
        event = None
        for candidate in unprocessed:
            cand_type = candidate.get("event_type", "")
            if event_type_filter and cand_type != event_type_filter:
                if subscription_id:
                    cand_evt_id = candidate.get("event_id", "")
                    if cand_evt_id:
                        _write_cursor(subscription_id, cand_evt_id)
                continue
            event = candidate
            break

        if not event:
            raise EventIgnoreError()

        event_id = event.get("event_id", "")
        event_seq = int(event.get("sequence_number", 0))
        event_type = event.get("event_type", "")
        event_payload = event.get("payload", {}) or {}
        event_metadata = event.get("metadata", {}) or {}
        message = event_payload.get("message", "") or event_payload.get("content", "") or event_payload.get("text", "")
        # request_id / conversation_key は composer_event_format.md §2 で metadata
        # に移行 (旧 publisher は payload 内に入れていたため互換維持の fallback)。
        request_id = event_metadata.get("request_id") or event_payload.get("request_id", "") or event_id
        conversation_key = event_metadata.get("conversation_key") or event_payload.get("conversation_key", "default")
        # composer_event_format.md §3-1: 同 group_id を持つ event 群は 1 論理
        # メッセージ。 Dify workflow 側で group_id 基準の集約を組みやすくする。
        group_id = event_metadata.get("group_id", "") or ""

        # 添付の解決: composer_event_format.md §2-2 では event-level の
        # payload_object_id + metadata.filename / content_type / size_bytes を
        # 標準とする。 旧 publisher の payload.attachments[] / payload.object_id
        # も後方互換として処理する。
        attachment_urls: list[str] = []
        evt_pob_id = event.get("payload_object_id", "")
        if evt_pob_id:
            try:
                url = get_payload_download_url(
                    base_url, client_id, client_secret, topic_id, evt_pob_id,
                )
                if url:
                    # metadata.filename を優先表示、 無ければ pob_id
                    label = event_metadata.get("filename") or evt_pob_id
                    attachment_urls.append(f"{label}: {url}")
            except Exception:
                pass
        # 旧 publisher の payload 内 attachments[] (legacy compat)
        for att in event_payload.get("attachments", []) or []:
            pob_id = att.get("payload_object_id") or att.get("object_id", "")
            if not pob_id:
                continue
            try:
                url = get_payload_download_url(
                    base_url, client_id, client_secret, topic_id, pob_id,
                )
                if url:
                    attachment_urls.append(f"{att.get('name', pob_id)}: {url}")
            except Exception:
                pass

        # Advance cursor to this event's event_id.
        if subscription_id and event_id:
            _write_cursor(subscription_id, event_id)

        return Variables(variables={
            "event_id": event_id,
            "sequence_number": event_seq,
            "event_type": event_type,
            "topic_id": topic_id,
            "message": message,
            "request_id": request_id,
            "conversation_key": conversation_key,
            "group_id": group_id,
            "payload_json": json.dumps(event_payload, ensure_ascii=False),
            "metadata_json": json.dumps(event_metadata, ensure_ascii=False),
            "attachment_urls": "\n".join(attachment_urls),
        })
