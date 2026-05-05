"""Read events from an AgenTrux topic."""
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage, ToolParameterOption

from provider.agentrux_api import build_topic_options, read_events


class ReadTool(Tool):
    def _fetch_parameter_options(self, parameter: str) -> list[ToolParameterOption]:
        if parameter != "topic_id":
            return []
        return [
            ToolParameterOption(label=o["label"], value=o["value"])
            for o in build_topic_options(self.runtime.credentials, {"read"})
        ]

    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        creds = self.runtime.credentials

        topic_id = tool_parameters.get("topic_id") or ""
        if not topic_id:
            yield self.create_text_message("topic_id is required")
            return

        limit = int(tool_parameters.get("limit", 10))
        event_type = tool_parameters.get("event_type")

        try:
            events = read_events(
                creds=creds,
                topic_id=topic_id,
                limit=limit,
                event_type=event_type,
            )
            yield self.create_json_message({"count": len(events), "events": events})
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(
                f"AgenTrux API error: {e.response.status_code}"
            )
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")
