"""List AgenTrux topics the current credential can reach.

Companion to publish/read/upload: because Dify's tool-node dynamic-select does
not fire its fetch callback (upstream bug, Dify #36518 / fix PR #36743), an
agent or a human first calls this tool to discover topic_id values, then passes
the chosen UUID to publish/read/upload.
"""
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from provider.agentrux_api import fetch_topics_raw, to_topics_payload

_VALID_ACTIONS = {"read", "write"}


class GetTopicsTool(Tool):
    def _invoke(
        self, tool_parameters: dict[str, Any]
    ) -> Generator[ToolInvokeMessage, None, None]:
        creds = self.runtime.credentials

        action = (tool_parameters.get("action") or "").strip().lower()
        if action and action not in _VALID_ACTIONS:
            yield self.create_text_message(
                f"action must be one of read/write (or empty for all); got {action!r}"
            )
            return
        allowed = {action} if action else _VALID_ACTIONS

        try:
            topics = fetch_topics_raw(creds, allowed)
            yield self.create_json_message(to_topics_payload(topics))
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(
                f"AgenTrux API error: {e.response.status_code}"
            )
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")
