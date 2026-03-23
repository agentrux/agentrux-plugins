# langflow-agentrux

> **Beta** -- API may change before 1.0.

Langflow components for [AgenTrux](https://github.com/agentrux/agentrux),
the A2A-authenticated short-lived data pipe service.

## Installation

```bash
pip install langflow-agentrux
```

Or install from source:

```bash
cd plugins/langflow
pip install -e .
```

## Components

| Component | Description |
|-----------|-------------|
| **AgenTrux Connection** | Authenticate with script_id + client_secret, optionally redeem a invite code. Produces a reusable client. |
| **AgenTrux Publish** | Publish a JSON event to a topic. |
| **AgenTrux List Events** | List events from a topic with optional type filter. |
| **AgenTrux Subscribe** | SSE subscription that collects events up to a max count or timeout. |

## Quick Start

1. Drag **AgenTrux Connection** onto the canvas.
2. Fill in `base_url`, `script_id`, and `client_secret`.
3. Connect its **Client** output to the **Connection** input of **Publish**,
   **List Events**, or **Subscribe**.
4. Configure the downstream component (topic_id, event_type, payload, etc.).
5. Run the flow.

## Environment Variables (alternative)

You can also set credentials via environment variables and reference them in
Langflow's global variables:

- `AGENTRUX_BASE_URL`
- `AGENTRUX_SCRIPT_ID`
- `AGENTRUX_CLIENT_SECRET`
- `AGENTRUX_INVITE_CODE` (optional)
