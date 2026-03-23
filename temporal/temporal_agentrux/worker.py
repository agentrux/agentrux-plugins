"""Worker initialization for the Temporal AgenTrux connector.

Reads credentials from environment variables and creates a shared
AgenTruxClient instance. The client is reused across all activity
invocations within the worker process.

Required environment variables:
    AGENTRUX_BASE_URL  - AgenTrux server URL (e.g. https://api.agentrux.com)
    AGENTRUX_SCRIPT_ID - Script ID for authentication
    AGENTRUX_SECRET    - Script secret for authentication

Optional environment variables:
    AGENTRUX_TIMEOUT_S - HTTP timeout in seconds (default: 30)
"""

from __future__ import annotations

import logging
import os

from agentrux.sdk.facade import AgenTruxClient

from .activities import (
    get_event_activity,
    list_events_activity,
    publish_event,
    wait_for_event,
)

logger = logging.getLogger("temporal_agentrux.worker")

_client: AgenTruxClient | None = None


def get_client() -> AgenTruxClient:
    """Get the shared AgenTruxClient instance.

    Raises RuntimeError if init_client() has not been called.
    """
    if _client is None:
        raise RuntimeError(
            "AgenTrux client not initialized. "
            "Call init_client() before running activities."
        )
    return _client


async def init_client() -> AgenTruxClient:
    """Initialize the shared AgenTruxClient from environment variables.

    Reads AGENTRUX_BASE_URL, AGENTRUX_SCRIPT_ID, and
    AGENTRUX_SECRET from the environment, obtains a JWT token,
    and creates a reusable client instance.

    Returns:
        The initialized AgenTruxClient.

    Raises:
        RuntimeError: If required environment variables are missing.
        httpx.HTTPStatusError: If token acquisition fails.
    """
    global _client

    base_url = os.environ.get("AGENTRUX_BASE_URL")
    script_id = os.environ.get("AGENTRUX_SCRIPT_ID")
    secret = os.environ.get("AGENTRUX_SECRET")
    timeout_s = float(os.environ.get("AGENTRUX_TIMEOUT_S", "30"))

    missing = []
    if not base_url:
        missing.append("AGENTRUX_BASE_URL")
    if not script_id:
        missing.append("AGENTRUX_SCRIPT_ID")
    if not secret:
        missing.append("AGENTRUX_SECRET")

    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    # Use a temporary client to obtain the JWT
    temp_client = AgenTruxClient(
        base_url=base_url,
        token="",  # No token yet
        timeout_s=timeout_s,
    )
    token_data = await temp_client.get_token(script_id, secret)
    await temp_client.close()

    access_token = token_data["access_token"]
    refresh_token = token_data.get("refresh_token")

    _client = AgenTruxClient(
        base_url=base_url,
        token=access_token,
        refresh_token=refresh_token,
        timeout_s=timeout_s,
    )

    logger.info(
        "AgenTrux client initialized for script %s at %s",
        script_id,
        base_url,
    )
    return _client


async def shutdown_client() -> None:
    """Close the shared client and release resources."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
        logger.info("AgenTrux client shut down")


def get_activities() -> list:
    """Return the list of activity functions to register with a Temporal worker.

    Usage:
        worker = Worker(
            client,
            task_queue="agentrux-queue",
            activities=get_activities(),
        )
    """
    return [
        publish_event,
        list_events_activity,
        get_event_activity,
        wait_for_event,
    ]
