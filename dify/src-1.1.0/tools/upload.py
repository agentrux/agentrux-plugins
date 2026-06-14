"""Upload a file to an AgenTrux topic."""
import base64
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from provider.agentrux_api import (
    create_payload,
    looks_like_topic_id,
    upload_to_presigned,
)


class UploadTool(Tool):
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

        filename = tool_parameters.get("filename") or "file.bin"
        content_type = tool_parameters.get("content_type") or "application/octet-stream"
        content_b64 = tool_parameters.get("content_base64") or ""

        try:
            raw = base64.b64decode(content_b64)
        except Exception:
            yield self.create_text_message("content_base64 is not valid base64")
            return

        try:
            payload = create_payload(
                creds=creds,
                topic_id=topic_id,
                content_type=content_type,
                filename=filename,
                size=len(raw),
            )
            yield self.create_json_message(payload)
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(_http_error_message(e))
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")


def _http_error_message(e: httpx.HTTPStatusError) -> str:
    code = e.response.status_code
    if code == 403:
        return (
            "AgenTrux API 403: this credential lacks write access to that topic. "
            "Call agentrux_get_topics(action=write) for writable topics."
        )
    if code == 404:
        return "AgenTrux API 404: topic not found. Re-check topic_id via agentrux_get_topics."
    return f"AgenTrux API error: {code}"
