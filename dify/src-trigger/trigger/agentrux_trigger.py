"""AgenTrux Trigger — receives hints from AgenTrux and dispatches to Event handlers.

Hint payload shape (cluster-agnostic ordering §3-3):
  { "topic_id": "...", "event_id": "evt_<uuid>", "cursor": "<opaque>",
    "timestamp": T, "delivery": "webhook" | "sse" }

旧 sequence_number フィールドは廃止 (ordering 非保証)。cursor は versioned opaque token
(内部 parse 禁止、?after= に pass-through するだけ)。

Two delivery modes (user 方針 2026-05-21: webhook 優先 / SSE fallback):
  - **webhook** (default、 推奨): AgenTrux backend POSTs hints to the Dify-allocated
                       endpoint. Plugin は stateless。 backend → plugin 到達性要 (public URL or
                       同 docker network)。 M9 配信 backend が動いている前提。
  - **sse** (fallback、 webhook 到達不可時): a background worker holds an outbound SSE
                       connection to AgenTrux per granted-read topic and POSTs each hint
                       to the endpoint locally (loopback)。 outbound HTTPS のみ必要 →
                       NAT/proxy 配下でも動作。 webhook が繋がらない環境のフォールバック。

Hint authenticity relies on the opaque per-subscription URL + TLS。 No shared
secret on the plugin side; matches the Dify built-in Webhook Trigger node's
no-secret model.
"""

from __future__ import annotations

import json
import logging
import time

from collections.abc import Mapping
from typing import Any

from werkzeug import Request, Response
from dify_plugin.entities.provider_config import CredentialType
from dify_plugin.entities.trigger import EventDispatch, Subscription, UnsubscribeResult
from dify_plugin.interfaces.trigger import Trigger, TriggerSubscriptionConstructor

from . import sse_worker

logger = logging.getLogger(__name__)


class AgentruxSubscriptionConstructor(TriggerSubscriptionConstructor):
    """Build a Subscription from the endpoint Dify allocates.

    AgenTrux webhook registration happens out-of-band (via the AgenTrux Console)
    because the plugin's Script credentials are Data-Plane scoped and cannot
    register webhooks themselves. The constructor just echoes the endpoint +
    properties back to Dify so Dify can persist the subscription.
    """

    def _validate_api_key(self, credentials: Mapping[str, Any]) -> None:
        # subscription_schema (base_url/activation_code) lives in properties,
        # not credentials. Credentials are empty for this plugin.
        return

    def _create_subscription(
        self,
        endpoint: str,
        parameters: Mapping[str, Any],
        credentials: Mapping[str, Any],
        credential_type: CredentialType,
    ) -> Subscription:
        # All user input lives in `credentials` (declared via subscription_schema).
        # `parameters` corresponds to subscription_constructor.parameters which we
        # leave empty in yaml, so we only read credentials.
        base_url = (credentials.get("base_url") or "").rstrip("/")
        activation_code = credentials.get("activation_code") or ""
        delivery_mode = (credentials.get("delivery_mode") or "webhook").strip()

        if delivery_mode == "sse":
            if not base_url or not activation_code:
                logger.error("sse mode requested but base_url/activation_code missing")
            else:
                try:
                    # AC is consumed here once and exchanged for persistent
                    # client_id/secret cached on disk. The event handler
                    # later recovers credentials via resolve_credentials_from_cache,
                    # so we deliberately omit activation_code from Subscription.properties
                    # (it is a secret-input and must not be persisted in Dify state).
                    sse_worker.start(endpoint, base_url, activation_code)
                except Exception as e:
                    logger.exception("sse worker start failed: %s", e)

        return Subscription(
            expires_at=-1,
            endpoint=endpoint,
            parameters=dict(parameters),
            # Only non-secret values go into properties. base_url is needed by
            # the event handler to look up credentials in the disk cache.
            properties={"base_url": base_url},
        )

    def _delete_subscription(
        self,
        subscription: Subscription,
        credentials: Mapping[str, Any],
        credential_type: CredentialType,
    ) -> UnsubscribeResult:
        try:
            sse_worker.stop(subscription.endpoint)
        except Exception as e:
            logger.warning("sse worker stop failed: %s", e)
        return UnsubscribeResult(
            success=True,
            message=(
                "Subscription removed locally. If you used webhook delivery, "
                "remove the corresponding webhook from AgenTrux Console too."
            ),
        )

    def _refresh_subscription(
        self,
        subscription: Subscription,
        credentials: Mapping[str, Any],
        credential_type: CredentialType,
    ) -> Subscription:
        # AgenTrux webhooks don't expire; nothing to refresh.
        return Subscription(
            expires_at=-1,
            endpoint=subscription.endpoint,
            parameters=subscription.parameters,
            properties=dict(subscription.properties or {}),
        )


class AgentruxTrigger(Trigger):
    def _dispatch_event(self, subscription: Subscription, request: Request) -> EventDispatch:
        # 1. Parse body
        raw_body = request.get_data()
        try:
            payload = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            return EventDispatch(
                events=[],
                response=Response('{"error": "invalid JSON"}', status=400, mimetype="application/json"),
            )

        # 2. Validate timestamp (reject hints older than 5 minutes)
        hint_ts = payload.get("timestamp", 0)
        if abs(time.time() - hint_ts) > 300:
            return EventDispatch(
                events=[],
                response=Response('{"error": "timestamp too old"}', status=400, mimetype="application/json"),
            )

        # 3. Dispatch to new_event handler (cluster-agnostic ordering §3-3)
        topic_id = payload.get("topic_id", "")
        event_id = payload.get("event_id", "")
        cursor = payload.get("cursor", "")
        # 旧 webhook が sequence_number を送ってくる場合の後方互換受信 (無視)。

        return EventDispatch(
            user_id=f"agentrux:{topic_id}",
            events=["new_event"],
            response=Response('{"status": "ok"}', status=200, mimetype="application/json"),
            payload={
                "topic_id": topic_id,
                "event_id": event_id,
                "cursor": cursor,
                # Do NOT pass client_secret here — Event Handler resolves
                # credentials from subscription.properties directly.
            },
        )
