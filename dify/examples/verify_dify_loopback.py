"""End-to-end verification of the Dify Topic2 → Topic3 workflow.

What it does:
  1. Publishes a test event to SOURCE Topic (Topic2 in the demo plan) with a
     unique `correlation_id`.
  2. Polls SINK Topic (Topic3) for a matching `correlation_id`.
  3. Reports PASS / FAIL with timing + the round-trip payload.

The Dify workflow under test:
  Trigger (subscribe SOURCE)
    └─ Code node (tag: processed_by=dify, processed_at=<iso>)
        └─ Tool (publish to SINK with correlation_id pass-through)

Pre-requisites (set BEFORE running):

  Environment variables:
    AGENTRUX_BASE_URL          e.g. https://api.agentrux.com
    AGENTRUX_CLIENT_ID         OAuth client_id (oauth-client_<uuid>) with
                               grants:  topic.write on SOURCE
                                        topic.read  on SINK
    AGENTRUX_CLIENT_SECRET     paired client_secret
    SOURCE_TOPIC_ID            top_<uuid> the Dify trigger subscribes to (=Topic2)
    SINK_TOPIC_ID              top_<uuid> the Dify tool publishes to   (=Topic3)

  In Dify:
    1. agentrux-trigger plugin v0.4.0 installed.
       Trigger connection authorised with an Activation Code that grants
       `topic.read` on SOURCE_TOPIC_ID, delivery_mode=sse, event_type_filter
       matches the EVENT_TYPE constant below (default "demo.request").
    2. agentrux-tools plugin v1.1.0 installed.
       Tools connection authorised with credentials granting `topic.write`
       on SINK_TOPIC_ID.
    3. dify/examples/topic2_to_topic3_pipeline.yml imported as a workflow,
       Trigger node + Publish node wired to the connections above,
       Publish node `topic_id` set to SINK_TOPIC_ID. Workflow published.

Usage:
    pip install agentrux-sdk
    python verify_dify_loopback.py

Exit code 0 on success, 1 on timeout / mismatch.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

from agentrux_sdk import AgentRuxClient

EVENT_TYPE = "demo.request"
POLL_INTERVAL_S = 1.0
TIMEOUT_S = 30


def _env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        sys.exit(f"missing env var: {name}")
    return val


async def main() -> int:
    base_url = _env("AGENTRUX_BASE_URL")
    client_id = _env("AGENTRUX_CLIENT_ID")
    client_secret = _env("AGENTRUX_CLIENT_SECRET")
    source = _env("SOURCE_TOPIC_ID")
    sink = _env("SINK_TOPIC_ID")

    correlation_id = f"verify-{uuid.uuid4().hex[:12]}"
    sent_payload = {
        "ping": "hello",
        "correlation_id": correlation_id,
        "sent_at": int(time.time()),
    }

    print(f"[verify] base_url={base_url}")
    print(f"[verify] source(Topic2)={source}")
    print(f"[verify] sink(Topic3)={sink}")
    print(f"[verify] correlation_id={correlation_id}")

    async with AgentRuxClient(
        endpoint=base_url, client_id=client_id, client_secret=client_secret
    ) as client:
        # 1) publish to SOURCE — Dify trigger should fire on this.
        t_send = time.monotonic()
        pub = await client.publish(
            topic_id=source, payload=sent_payload, event_type=EVENT_TYPE
        )
        print(f"[verify] published evt={pub.event_id} seq={pub.sequence_number}")

        # 2) poll SINK until a matching correlation_id arrives, or timeout.
        deadline = t_send + TIMEOUT_S
        cursor: str | None = None
        while time.monotonic() < deadline:
            async for evt in client.read_pull(
                topic_id=sink, after=cursor, limit=50, stop_when_empty=True
            ):
                cursor = evt.event_id
                payload = evt.payload or {}
                if not isinstance(payload, dict):
                    continue
                if payload.get("correlation_id") == correlation_id:
                    elapsed_ms = int((time.monotonic() - t_send) * 1000)
                    print(f"[verify] PASS — round-trip in {elapsed_ms} ms")
                    print(
                        f"[verify] sink event_id={evt.event_id} "
                        f"event_type={evt.event_type} seq={evt.sequence_number}"
                    )
                    print(
                        f"[verify] sink payload="
                        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
                    )
                    return 0
            await asyncio.sleep(POLL_INTERVAL_S)

        print(
            f"[verify] FAIL — no matching correlation_id on sink "
            f"within {TIMEOUT_S}s"
        )
        print("[verify] checklist:")
        print("  - Dify workflow imported AND published?")
        print("  - Trigger connection AC has topic.read on SOURCE_TOPIC_ID?")
        print("  - Trigger event_type_filter matches " f"'{EVENT_TYPE}' (or empty)?")
        print(
            "  - Trigger delivery_mode=sse (NAT-friendly) and Dify can reach "
            "AgenTrux SSE?"
        )
        print("  - Publish node topic_id == SINK_TOPIC_ID?")
        print("  - Tools connection has topic.write on SINK_TOPIC_ID?")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
