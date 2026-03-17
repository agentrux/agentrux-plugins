"""AgenTrux Subscribe Component for Langflow.

Opens an SSE subscription to a topic and yields incoming events as Data
items.  Because Langflow components run as one-shot by default, this
component collects events until *max_events* is reached or *timeout_seconds*
elapses, then returns the batch.
"""
from __future__ import annotations

import asyncio
import logging

from langflow.custom import Component
from langflow.io import DataInput, IntInput, MessageTextInput, Output
from langflow.schema import Data

logger = logging.getLogger(__name__)


class AgenTruxSubscribeComponent(Component):
    display_name = "AgenTrux Subscribe"
    description = "Subscribe to an AgenTrux topic (SSE) and collect events."
    icon = "radio"
    name = "AgenTruxSubscribe"

    inputs = [
        DataInput(
            name="connection",
            display_name="Connection",
            info="AgenTrux Connection output (Data containing the client).",
            required=True,
        ),
        MessageTextInput(
            name="topic_id",
            display_name="Topic ID",
            info="Topic UUID to subscribe to.",
            required=True,
        ),
        IntInput(
            name="max_events",
            display_name="Max Events",
            info="Stop after receiving this many events.",
            value=10,
            required=False,
        ),
        IntInput(
            name="timeout_seconds",
            display_name="Timeout (seconds)",
            info="Stop after this many seconds even if max_events not reached.",
            value=30,
            required=False,
        ),
        MessageTextInput(
            name="event_type",
            display_name="Event Type Filter",
            info="Optional event type to filter (client-side).",
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="Events",
            name="events",
            method="subscribe_events",
        ),
    ]

    async def subscribe_events(self) -> list[Data]:
        """Subscribe via SSE and collect events up to limits."""
        from agentrux.sdk.facade import AgenTruxClient

        client: AgenTruxClient = self.connection.data["client"]
        max_events: int = self.max_events or 10
        timeout: int = self.timeout_seconds or 30
        event_type_filter: str | None = self.event_type or None

        results: list[Data] = []

        sub = client.subscribe(
            topic_id=self.topic_id,
            mode="sse",
        )

        async def _collect() -> None:
            async with sub:
                async for env in sub:
                    # Client-side event type filter
                    if event_type_filter and env.type != event_type_filter:
                        continue

                    results.append(
                        Data(
                            data={
                                "event_id": env.event_id,
                                "sequence_no": env.sequence_no,
                                "timestamp": (
                                    env.timestamp.isoformat()
                                    if env.timestamp
                                    else None
                                ),
                                "type": env.type,
                                "payload": env.payload,
                                "payload_ref": env.payload_ref,
                                "producer_script": env.producer_script,
                            }
                        )
                    )

                    if len(results) >= max_events:
                        break

        try:
            await asyncio.wait_for(_collect(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info(
                "Subscribe timeout after %ds with %d events collected",
                timeout,
                len(results),
            )

        logger.info(
            "Subscription to topic %s collected %d events",
            self.topic_id,
            len(results),
        )

        return results
