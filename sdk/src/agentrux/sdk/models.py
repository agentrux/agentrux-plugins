"""SDK 内部で使う Pydantic schema.

Phase 5.2 では skeleton 定義のみ。 5.3 以降で field 詳細を確定。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TokenResponse(_Frozen):
    """POST /oauth/token のレスポンス (client_credentials grant)."""

    access_token: str           # "aat_<JWT>"
    token_type: str             # "Bearer"
    expires_in: int             # 秒、 server 側で 600 等
    scope: str = ""             # space-delimited、 default 空


class PublishResult(_Frozen):
    """publish 成功時の返り値."""

    event_id: str               # "evt_<uuid>"
    sequence_number: int
    idempotent_replayed: bool   # True なら server 側 replay


class Event(_Frozen):
    """read で取得した event."""

    event_id: str
    topic_id: str
    event_type: str
    sequence_number: int
    occurred_at: datetime
    payload: dict | None = None              # inline の場合のみ
    payload_object_ref: str | None = None    # "pob_<uuid>"、 object_ref の場合のみ
    metadata: dict | None = None
