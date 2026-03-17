"""AgenTrux List Events Component for Langflow.

Retrieves events from a topic via an authenticated AgenTruxClient and
returns each MessageEnvelope as an individual Data item.
"""
from __future__ import annotations

import logging

from langflow.custom import Component
from langflow.io import DataInput, IntInput, MessageTextInput, Output
from langflow.schema import Data

logger = logging.getLogger(__name__)


class AgenTruxListEventsComponent(Component):
    display_name = "AgenTrux List Events"
    description = "List events from an AgenTrux topic."
    icon = "list"
    name = "AgenTruxListEvents"

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
            info="Topic UUID to list events from.",
            required=True,
        ),
        IntInput(
            name="limit",
            display_name="Limit",
            info="Maximum number of events to retrieve.",
            value=50,
            required=False,
        ),
        MessageTextInput(
            name="event_type",
            display_name="Event Type Filter",
            info="Optional event type to filter by.",
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="Events",
            name="events",
            method="list_events",
        ),
    ]

    async def list_events(self) -> list[Data]:
        """Fetch events and return a list of Data objects."""
        from agentrux.sdk.facade import AgenTruxClient

        client: AgenTruxClient = self.connection.data["client"]

        kwargs: dict = {
            "limit": self.limit or 50,
        }
        if self.event_type:
            kwargs["event_type"] = self.event_type

        envelopes, _cursor = await client.list_events(
            topic_id=self.topic_id,
            **kwargs,
        )

        logger.info(
            "Listed %d events from topic %s",
            len(envelopes),
            self.topic_id,
        )

        results: list[Data] = []
        for env in envelopes:
            results.append(
                Data(
                    data={
                        "event_id": env.event_id,
                        "sequence_no": env.sequence_no,
                        "timestamp": env.timestamp.isoformat() if env.timestamp else None,
                        "type": env.type,
                        "payload": env.payload,
                        "payload_ref": env.payload_ref,
                        "producer_script": env.producer_script,
                    }
                )
            )

        return results
