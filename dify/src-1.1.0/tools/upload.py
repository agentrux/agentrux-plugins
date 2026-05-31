"""Upload a file to an AgenTrux topic."""

import base64
from collections.abc import Generator
from typing import Any

import httpx
from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage, ToolParameterOption

from provider.agentrux_api import (
    build_topic_options,
    create_payload,
    upload_to_presigned,
)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB
_MAX_BASE64_INPUT = 20 * 1024 * 1024  # ~20 MB (base64 overhead)


class UploadTool(Tool):
    def _fetch_parameter_options(self, parameter: str) -> list[ToolParameterOption]:
        if parameter != "topic_id":
            return []
        return [
            ToolParameterOption(label={"en_US": o["label"], "ja_JP": o["label"]}, value=o["value"])
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

        filename = tool_parameters["filename"]
        content_type = tool_parameters["content_type"]

        content_b64 = tool_parameters["content_base64"]
        if len(content_b64) > _MAX_BASE64_INPUT:
            yield self.create_text_message(
                f"base64 input too large ({len(content_b64)} bytes). "
                f"Maximum is {_MAX_BASE64_INPUT} bytes."
            )
            return

        try:
            data = base64.b64decode(content_b64, validate=True)
        except Exception as e:
            yield self.create_text_message(f"Invalid base64 content: {e}")
            return

        if len(data) > MAX_UPLOAD_BYTES:
            yield self.create_text_message(
                f"File too large ({len(data)} bytes). "
                f"Maximum is {MAX_UPLOAD_BYTES} bytes (15 MB)."
            )
            return

        try:
            result = create_payload(
                creds=creds,
                topic_id=topic_id,
                content_type=content_type,
                filename=filename,
                size=len(data),
            )
            upload_to_presigned(result["upload_url"], data, content_type)

            yield self.create_json_message(
                {
                    "object_id": result["object_id"],
                    "download_url": result.get("download_url", ""),
                }
            )
        except httpx.HTTPStatusError as e:
            yield self.create_text_message(
                f"AgenTrux API error: {e.response.status_code}"
            )
        except Exception as e:
            yield self.create_text_message(f"AgenTrux error: {e}")
