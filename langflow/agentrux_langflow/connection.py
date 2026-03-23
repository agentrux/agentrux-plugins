"""AgenTrux Connection Component for Langflow.

Centralised authentication: obtains a JWT via get_token() and optionally
redeems a share code, then exposes an initialised AgenTruxClient as
output Data so downstream components can reuse the same session.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from langflow.custom import Component
from langflow.io import MessageTextInput, Output, SecretStrInput
from langflow.schema import Data

logger = logging.getLogger(__name__)


class AgenTruxConnectionComponent(Component):
    display_name = "AgenTrux Connection"
    description = "Authenticate with AgenTrux and produce a reusable client."
    icon = "link"
    name = "AgenTruxConnection"

    inputs = [
        MessageTextInput(
            name="base_url",
            display_name="Base URL",
            info="AgenTrux server URL (e.g. https://api.agentrux.com)",
            required=True,
        ),
        MessageTextInput(
            name="script_id",
            display_name="Script ID",
            info="Script identifier for authentication.",
            required=True,
        ),
        SecretStrInput(
            name="client_secret",
            display_name="Client Secret",
            info="Script API key for authentication.",
            required=True,
        ),
        SecretStrInput(
            name="invite_code",
            display_name="Invite Code",
            info="Optional share code for cross-account access.",
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="Client",
            name="client",
            method="build_client",
        ),
    ]

    async def _authenticate(self) -> dict[str, Any]:
        """Obtain JWT and optionally redeem share code."""
        from agentrux.sdk.facade import AgenTruxClient

        base_url: str = self.base_url
        script_id: str = self.script_id
        client_secret: str = self.client_secret
        invite_code: str | None = self.invite_code or None

        # Use a temporary client just for auth endpoints
        temp_client = AgenTruxClient(base_url=base_url, token="")

        # Redeem share code first if provided
        if invite_code:
            logger.info("Redeeming share code for script %s", script_id)
            await temp_client.redeem_grant(
                invite_code=invite_code,
                script_id=script_id,
                client_secret=client_secret,
            )

        # Obtain JWT
        token_data = await temp_client.get_token(script_id, client_secret)
        await temp_client.close()
        return token_data

    async def build_client(self) -> Data:
        """Authenticate and return a Data object wrapping the live client."""
        from agentrux.sdk.facade import AgenTruxClient

        token_data = await self._authenticate()

        client = AgenTruxClient(
            base_url=self.base_url,
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
        )

        return Data(
            data={
                "client": client,
                "base_url": self.base_url,
                "script_id": self.script_id,
                "expires_at": token_data.get("expires_at", ""),
            }
        )
