"""Composer Event Group helper — receiver-side iterator (Phase BT.1.d 部分実装).

SSOT: docs/04_design/messaging/composer_event_format.md §3-3 (Receiver/renderer 責務).

**SDK scope (3 軸、 memory `feedback_sdk_scope`)**: 本 helper は SDK scope 3 軸を
すべて満たす:

  1. endpoint 軸: `/topics/{top_id}/*` Data Plane (上流 `Event` 取得経路) + `/oauth/*`
     Auth plane (上流 client が `aat_` を取得する経路)、 `/console/*` / `/admin/*`
     には触れない
  2. token 軸: 上流 stream は `aat_` 経由で取得されたものを想定。 本 helper 自体は
     token を保持しない (pure transformation)
  3. UX 依存軸: なし (本 helper 内に approve flow は無い、 上流の publisher 側で
     既に確立済の event を受け取るだけ)

役割:
    SDK consumer (Python script / agent / 経路 B plugin) が `client.read_*()` で
    取得した Data Plane event stream をそのまま timeline 描画するのではなく、
    `metadata.group_id` で集約して「1 メッセージ = 1 ComposerGroup」 単位で受け取れる
    ようにする helper。

設計判断:
    - publisher 側 helper は **本 file では未実装**。 SDK low-level `publish()` が
      内部で S3 PUT を行う設計 (raw bytes → presigned PUT → commit を 1 関数で完結)
      のため、 stage-then-send (S3 PUT 済 `payload_object_id` を event commit のみ
      する経路) を組み立てるには Data Plane low-level API の分割 (`upload_payload()` /
      `publish_object_ref()`、 いずれも `/topics/{top_id}/*` Data Plane) が前提となる。
      別 PR で着手予定。 Console / Admin endpoint には触れない (SDK scope 3 軸 #1)。
    - 本 file は read-side only。 既存 `Event` 受領経路に依存し、 新規 low-level
      API を追加しない (CLAUDE.md §最優先原則 メンテナンス性 + 早すぎる抽象化禁止)。

アルゴリズム (§3-3):
    1. 入力 event を順番に消費し、 `metadata.group_id` がある event は group buffer に蓄積
    2. 同 group 内に `composer.text | composer.json` を見つけたら、 同 group の
       `composer.upload` 群と合わせて 1 ComposerGroup として yield (= 1 bubble)
    3. group buffer は **flush_timeout_seconds 秒で flush** (text/json が来ないまま
       タイムアウトしたら upload 群を text_event=None で yield)
    4. `metadata.group_id` が無い event は単独 ComposerGroup として即時 yield
    5. 異 publisher (異 producer_script_id / console_user_id) で同 group_id は別 group 扱い
       (group_id 衝突は spec で bug、 本実装では publisher 区別 key で別 buffer)
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from pydantic import ConfigDict

from agentrux_sdk.models import Event, _Frozen


class ComposerGroup(_Frozen):
    """`iter_composer_groups()` が yield する 1 group 単位 (= 1 bubble 単位)。

    Fields:
        group_id: `metadata.group_id` の値 (UUIDv4)、 standalone 経路は None。
        text_event: `composer.text` または `composer.json` event (group 内に最大 1 件)、
            text 不在の attachment-only group は None。
        upload_events: 同 group_id 内の `composer.upload` event 群 (空 tuple もあり得る)。
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    group_id: str | None
    text_event: Event | None
    upload_events: tuple[Event, ...]


def _publisher_key(event: Event) -> str:
    """異 publisher の同 group_id を別 group 扱いするための key (§3-3 step 5)。

    現 `Event` model に producer 識別子は無いので payload / metadata から間接的に
    取り出す路はないため、 本実装は `group_id` 単独を key に使う (= 同 group_id は
    1 group 扱い、 衝突は呼び出し側が warning log で検出する責務)。 model 拡張は別 PR。
    """
    md = event.metadata or {}
    return str(md.get("group_id") or "")


def _is_text_kind(event: Event) -> bool:
    return event.event_type in ("composer.text", "composer.json")


def _is_upload_kind(event: Event) -> bool:
    return event.event_type == "composer.upload"


def _group_id_of(event: Event) -> str | None:
    md = event.metadata
    if not isinstance(md, dict):
        return None
    gid = md.get("group_id")
    return str(gid) if isinstance(gid, str) and gid else None


async def iter_composer_groups(
    events: AsyncIterator[Event],
    *,
    flush_timeout_seconds: float = 60.0,
) -> AsyncIterator[ComposerGroup]:
    """event stream を `metadata.group_id` で集約し、 1 group = 1 bubble 単位で yield.

    Args:
        events: 上流 event stream (例: `client.read_hybrid(topic_id=...)`).
            event は時系列順 (`sequence_number` 昇順) で渡される前提。
        flush_timeout_seconds: text/json 不着のまま flush するまでの秒数。 spec §3-3
            は 60 秒。 0 を指定すると flush しない (= text/json が来るまで永遠に保留)。

    Yields:
        ComposerGroup: 1 つ取り出すごとに 1 bubble 相当。 standalone (group_id 無し
        OR composer 以外の event_type) は単独 group で即時 yield される。

    Note:
        - 上流 stream が close したとき buffer に残っている group は upload-only として
          flush して yield する (text が来ない確定状態とみなす)。
        - flush は時刻ベースで毎 event 受領時に判定する (background task は使わない、
          asyncio 不変の確定性のため)。
        - composer 以外の event_type (例: `user.event`) は単独 group として即時 yield。
    """
    # group_id → (upload_events, first_seen_monotonic) の buffer
    buffer: dict[str, tuple[list[Event], float]] = {}

    def _drain_expired(now: float) -> list[ComposerGroup]:
        """flush_timeout を過ぎた group を upload-only group として返す。"""
        if flush_timeout_seconds <= 0:
            return []
        expired: list[ComposerGroup] = []
        to_drop: list[str] = []
        for gid, (uploads, first_seen) in buffer.items():
            if now - first_seen >= flush_timeout_seconds:
                expired.append(
                    ComposerGroup(group_id=gid, text_event=None, upload_events=tuple(uploads))
                )
                to_drop.append(gid)
        for gid in to_drop:
            buffer.pop(gid, None)
        return expired

    async for event in events:
        now = time.monotonic()

        # 1. 時刻ベース flush 判定 (event 受領契機、 background task 不要)
        for expired in _drain_expired(now):
            yield expired

        # 2. event 種別ごとの分岐
        gid = _group_id_of(event)
        if gid is None:
            # group_id 無し → 単独 group で即時 yield (§3-3 step 5、 後方互換)
            yield ComposerGroup(
                group_id=None,
                text_event=event if _is_text_kind(event) else None,
                upload_events=(event,) if _is_upload_kind(event) else (),
            )
            continue

        if _is_text_kind(event):
            # text/json 着信 → 同 group の upload 群 + 当該 text を 1 group として yield
            uploads, _ = buffer.pop(gid, ([], now))
            yield ComposerGroup(
                group_id=gid,
                text_event=event,
                upload_events=tuple(uploads),
            )
        elif _is_upload_kind(event):
            # upload 着信 → buffer に蓄積 (text 着信 or flush_timeout 経過で yield)
            if gid in buffer:
                uploads, first_seen = buffer[gid]
                uploads.append(event)
                buffer[gid] = (uploads, first_seen)
            else:
                buffer[gid] = ([event], now)
        else:
            # composer 以外の event_type が group_id 付きで来た場合は standalone 扱い
            # (spec は composer.text / json / upload に限定、 後方互換のための保険)
            yield ComposerGroup(group_id=gid, text_event=None, upload_events=())

    # 上流 stream 終端: buffer に残っている group を upload-only として flush
    for gid, (uploads, _) in buffer.items():
        yield ComposerGroup(group_id=gid, text_event=None, upload_events=tuple(uploads))


__all__ = ["ComposerGroup", "iter_composer_groups"]
