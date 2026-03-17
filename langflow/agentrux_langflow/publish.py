"""AgenTrux Publish Event Component for Langflow.

Publishes an event to a topic via an authenticated AgenTruxClient and
returns the resulting event_id as output Data.
"""
from __future__ import annotations

import json
import logging

from langflow.custom import Component
from langflow.io import DataInput, MessageTextInput, Output
from langflow.schema import Data

logger = logging.getLogger(__name__)


class AgenTruxPublishComponent(Component):
    display_name = "AgenTrux Publish"
    description = "Publish an event to an AgenTrux topic."
    icon = "upload"
    name = "AgenTruxPublish"

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
            info="Target topic UUID.",
            required=True,
        ),
        MessageTextInput(
            name="event_type",
            display_name="Event Type",
            info="Event type string (e.g. 'sensor.reading').",
            required=True,
        ),
        MessageTextInput(
            name="payload",
            display_name="Payload (JSON)",
            info="JSON string representing the event payload.",
            required=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Result",
            name="result",
            method="publish_event",
        ),
    ]

    async def publish_event(self) -> Data:
        """Publish an event and return the event_id."""
        from agentrux.sdk.facade import AgenTruxClient

        client: AgenTruxClient = self.connection.data["client"]

        # Parse JSON payload
        try:
            payload_dict = json.loads(self.payload) if self.payload else None
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Invalid JSON payload: {exc}. "
                "Provide a valid JSON string, e.g. '{\"key\": \"value\"}'."
            ) from exc

        event_id = await client.publish(
            topic_id=self.topic_id,
            event_type=self.event_type,
            payload=payload_dict,
        )

        logger.info(
            "Published event %s to topic %s (type=%s)",
            event_id,
            self.topic_id,
            self.event_type,
        )

        return Data(
            data={
                "event_id": event_id,
                "topic_id": self.topic_id,
                "event_type": self.event_type,
            }
        )
