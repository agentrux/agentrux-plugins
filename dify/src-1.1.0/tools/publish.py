"""Publish an event to an AgenTrux topic."""
import json
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage, ToolParameterOption

from provider.agentrux_api import build_topic_options, publish_event


class PublishTool(Tool):
    def _fetch_parameter_options(self, parameter: str) -> list[ToolParameterOption]:
        if parameter != "topic_id":
            return []
        return [
            ToolParameterOption(label=o["label"], value=o["value"])
            for o in build_topic_options(self.runtime.credentials, {"write"})
        ]

    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        creds = self.runtime.credentials

        topic_id = tool_parameters.get("topic_id") or ""
        if not topic_id:
            yield self.create_text_message("topic_id is required")
            return

        event_type = tool_parameters.get("event_type") or "dify.message"
        payload_str = tool_parameters.get("payload_json") or "{}"
        try:
            payload = (
                json.loads(payload_str) if isinstance(payload_str, str) else payload_str
            )
        except json.JSONDecodeError:
            payload = {"raw": payload_str}

        correlation_id = tool_parameters.get("correlation_id") or None

        try:
            result = publish_event(
                creds=creds,
                topic_id=topic_id,
                event_type=event_type,
                payload=payload,
                correlation_id=correlation_id,
            )
            yield self.create_json_message(result)
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(
                f"AgenTrux API error: {e.response.status_code}"
            )
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")
