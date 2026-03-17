# temporal-agentrux

Temporal Activities for AgenTrux event operations.

**Status: Beta**

## Install

```bash
pip install -e plugins/temporal
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `AGENTRUX_BASE_URL` | Yes | AgenTrux server URL |
| `AGENTRUX_SCRIPT_ID` | Yes | Script ID for authentication |
| `AGENTRUX_SECRET` | Yes | Script secret for authentication |
| `AGENTRUX_TIMEOUT_S` | No | HTTP timeout in seconds (default: 30) |

Secrets are read from environment variables at worker startup. They are never
passed as activity arguments.

## Worker Setup

```python
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from temporal_agentrux.worker import init_client, shutdown_client, get_activities

async def main():
    # Initialize the shared AgenTrux client (reads env vars, obtains JWT)
    await init_client()

    # Connect to Temporal
    temporal_client = await Client.connect("localhost:7233")

    # Start the worker with AgenTrux activities registered
    worker = Worker(
        temporal_client,
        task_queue="agentrux-queue",
        activities=get_activities(),
    )
    try:
        await worker.run()
    finally:
        await shutdown_client()

asyncio.run(main())
```

## Activities

### publish_event

Publish an event to an AgenTrux topic.

- **Input**: `PublishInput(topic_id, event_type, payload)`
- **Output**: `PublishResult(event_id)`

### list_events_activity

List events from a topic with optional filtering and pagination.

- **Input**: `ListEventsInput(topic_id, limit=50, cursor=None, event_type=None)`
- **Output**: `ListEventsResult(events, next_cursor)`

### get_event_activity

Get a single event by ID.

- **Input**: `GetEventInput(topic_id, event_id)`
- **Output**: `dict` with event fields

### wait_for_event

Subscribe via SSE and wait for a matching event. Sends periodic heartbeats
to Temporal to prevent activity timeout.

- **Input**: `WaitInput(topic_id, event_type=None, timeout_seconds=300, heartbeat_interval_seconds=10)`
- **Output**: `WaitResult(found, event)`

## Example Workflow

```python
from datetime import timedelta
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from temporal_agentrux import (
        PublishInput, PublishResult,
        WaitInput, WaitResult,
    )

@workflow.defn
class EventPipeline:
    @workflow.run
    async def run(self, topic_id: str) -> str:
        # Publish an event
        result = await workflow.execute_activity(
            "publish_event",
            PublishInput(topic_id=topic_id, event_type="pipeline.started", payload={"step": 1}),
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Wait for a response event
        wait_result = await workflow.execute_activity(
            "wait_for_event",
            WaitInput(topic_id=topic_id, event_type="pipeline.completed", timeout_seconds=120),
            start_to_close_timeout=timedelta(seconds=150),
            heartbeat_timeout=timedelta(seconds=30),
        )

        if wait_result.found:
            return f"Completed: {wait_result.event.event_id}"
        return "Timed out waiting for completion"
```
