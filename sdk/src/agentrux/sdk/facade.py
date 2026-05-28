"""AgentRuxClient — L1 high-level facade.

SSOT: docs/04_design/sdk/sdk_design.md §1 設計原則 (5) 3 階層 Public API
"""

from __future__ import annotations

from typing import Any

import httpx

from agentrux.sdk.auth import Authenticator
from agentrux.sdk.config import SDKConfig
from agentrux.sdk.http_client import HTTPClient


class AgentRuxClient:
    """典型的な script ユーザーが import する 1 つの class.

    使い方:
      async with AgentRuxClient(
          endpoint="https://api.agentrux.io",
          client_id="crd_...",
          client_secret="aks_...",
      ) as client:
          # 5.4 で実装
          await client.publish(topic_id="top_X", payload={"k": "v"})
          # 5.5 で実装
          async for evt in client.read_hybrid(topic_id="top_Y"):
              ...
    """

    def __init__(
        self,
        *,
        endpoint: str,
        client_id: str,
        client_secret: str,
        **config_kwargs: Any,
    ) -> None:
        self._config = SDKConfig(
            endpoint=endpoint,
            client_id=client_id,
            client_secret=client_secret,
            **config_kwargs,
        )
        self._http = HTTPClient(self._config)
        self._auth = Authenticator(self._config, self._http)

    @property
    def config(self) -> SDKConfig:
        return self._config

    @property
    def auth(self) -> Authenticator:
        return self._auth

    @property
    def http(self) -> HTTPClient:
        return self._http

    # ------------------------------------------------------------------
    # 5.3 共通 request (publish/read から使用)
    # ------------------------------------------------------------------
    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """authenticated + retry 付き request. 5.4 以降の publish/read の primitive."""
        return await self._http.request_with_retry(
            method, path, auth=self._auth, **kwargs
        )

    # ------------------------------------------------------------------
    # 5.4 publish
    # ------------------------------------------------------------------
    async def publish(
        self,
        *,
        topic_id: str,
        payload: Any,
        event_type: str | None = None,
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ):
        """inline / object_ref 自動切替 publish (sdk_design.md §4)."""
        from agentrux.sdk.publish import publish as _publish

        return await _publish(
            self,
            topic_id=topic_id,
            payload=payload,
            event_type=event_type,
            idempotency_key=idempotency_key,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # 5.5 read 3 modes
    # ------------------------------------------------------------------
    def read_pull(
        self,
        *,
        topic_id: str,
        after: str | None = None,
        limit: int = 100,
        poll_interval_seconds: float = 1.0,
        stop_when_empty: bool = False,
    ):
        from agentrux.sdk.pull_client import read_pull as _read_pull

        return _read_pull(
            self,
            topic_id=topic_id,
            after=after,
            limit=limit,
            poll_interval_seconds=poll_interval_seconds,
            stop_when_empty=stop_when_empty,
        )

    def read_sse(
        self,
        *,
        topic_id: str,
        last_event_id: str | None = None,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 3,
    ):
        from agentrux.sdk.sse_client import read_sse as _read_sse

        return _read_sse(
            self,
            topic_id=topic_id,
            last_event_id=last_event_id,
            auto_reconnect=auto_reconnect,
            max_reconnect_attempts=max_reconnect_attempts,
        )

    def read_hybrid(
        self,
        *,
        topic_id: str,
        last_event_id: str | None = None,
        poll_interval_seconds: float = 1.0,
        limit: int = 100,
    ):
        from agentrux.sdk.hybrid_consumer import read_hybrid as _read_hybrid

        return _read_hybrid(
            self,
            topic_id=topic_id,
            last_event_id=last_event_id,
            poll_interval_seconds=poll_interval_seconds,
            limit=limit,
        )

    # ------------------------------------------------------------------
    # 5.6 pipeline
    # ------------------------------------------------------------------
    def pipeline(
        self,
        *,
        source_topic: str,
        sink_topic: str,
        transform,
        checkpoint_store=None,
        gap_detector=None,
        reorder_buffer=None,
        mode: str = "pull",
        pull_limit: int = 100,
        pull_interval_seconds: float = 1.0,
    ):
        from agentrux.sdk.pipeline import Pipeline

        return Pipeline(
            self,
            source_topic=source_topic,
            sink_topic=sink_topic,
            transform=transform,
            checkpoint_store=checkpoint_store,
            gap_detector=gap_detector,
            reorder_buffer=reorder_buffer,
            mode=mode,
            pull_limit=pull_limit,
            pull_interval_seconds=pull_interval_seconds,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AgentRuxClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
