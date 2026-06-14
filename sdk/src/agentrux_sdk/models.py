"""SDK 内部で使う Pydantic schema.

SSOT: docs/04_design/sdk/sdk_design.md §5 / §6,
      docs/04_design/messaging/cluster_agnostic_ordering.md §3-3

Phase 5.x → cluster-agnostic モデルへの rebase:
  - Event.sequence_number 削除 (seq 連番保証を撤回)
  - Event.cursor 追加 (per-event opaque cursor、 resume に使う、行存在非依存)
  - PublishResult.sequence_number 削除
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TokenResponse(_Frozen):
    """POST /oauth/token のレスポンス (client_credentials grant)."""

    access_token: str  # "aat_<JWT>"
    token_type: str  # "Bearer"
    expires_in: int  # 秒、 server 側で 600 等
    scope: str = ""  # space-delimited、 default 空


class PublishResult(_Frozen):
    """publish 成功時の返り値."""

    event_id: str  # "evt_<uuid>"
    idempotent_replayed: bool = False  # True なら server 側 replay


class Event(_Frozen):
    """read で取得した event.

    field 名は server / SSOT (read_flow.md §event item) に一致させる:
      - stored_at: server が返す保存時刻
      - payload_object_id: object_ref event の "pob_<uuid>"
      - cursor: per-event opaque cursor (server 署名付き、 created_at 内包)。
                checkpoint に保存して resume 位置として使う。行存在に依存しない。
                値は cluster_agnostic_ordering.md §3-3 の versioned token 形式。
    """

    event_id: str
    topic_id: str
    event_type: str
    stored_at: datetime
    payload: Any = None  # inline の場合のみ (任意 JSON 値)
    payload_object_id: str | None = None  # "pob_<uuid>"、 object_ref の場合のみ
    metadata: dict[str, Any] | None = None
    cursor: str = ""  # per-event opaque cursor (空文字は未対応 server との後方互換)
