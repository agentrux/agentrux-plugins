"""Read events from an AgenTrux topic."""
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from provider.agentrux_api import looks_like_topic_id, read_events


class ReadTool(Tool):
    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        creds = self.runtime.credentials

        topic_id = (tool_parameters.get("topic_id") or "").strip()
        if not topic_id:
            yield self.create_text_message(
                "topic_id is required. Call agentrux_get_topics first to discover it."
            )
            return
        if not looks_like_topic_id(topic_id):
            yield self.create_text_message(
                f"topic_id {topic_id!r} is not a valid AgenTrux topic id "
                "(expected top_<uuid> or a UUID from agentrux_get_topics)."
            )
            return

        # cluster-agnostic ordering §3-3: cursor は opaque token / evt_<id>。
        # 旧 after_sequence_no (int) は廃止 → after (string cursor) に移行。
        after = (tool_parameters.get("after") or tool_parameters.get("after_sequence_no") or "").strip() or None
        limit = tool_parameters.get("limit") or 10
        event_type = tool_parameters.get("event_type") or None

        try:
            events = read_events(
                creds=creds,
                topic_id=topic_id,
                after=after,
                limit=int(limit),
                event_type=event_type,
            )
            yield self.create_json_message({"count": len(events), "events": events})
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(_http_error_message(e))
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")


def _http_error_message(e: httpx.HTTPStatusError) -> str:
    code = e.response.status_code
    if code == 403:
        return (
            "AgenTrux API 403: this credential lacks read access to that topic. "
            "Call agentrux_get_topics(action=read) for readable topics."
        )
    if code == 404:
        return "AgenTrux API 404: topic not found. Re-check topic_id via agentrux_get_topics."
    return f"AgenTrux API error: {code}"
