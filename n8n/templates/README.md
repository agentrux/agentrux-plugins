# AgenTrux n8n — workflow templates

Import-ready n8n workflows that show common AgenTrux patterns. In n8n:
**Workflows → ⋯ (top-right) → Import from File** → pick a `*.workflow.json`.

After importing:
1. Open each **AgenTrux** / **AgenTrux Trigger** node and select your **AgenTrux API** credential.
2. Replace every `top_REPLACE_ME` with your Topic ID (or pick it from the dropdown).

> **Node type names.** These templates use the published community-node type names
> (`@agentrux/n8n-nodes-agentrux.agenTrux` / `…​.agenTruxTrigger`). If you installed the
> node the manual way (built into `~/.n8n/custom`), n8n registers it under the **`CUSTOM`**
> namespace instead — the imported nodes will show as "unrecognized". In that case either
> install from npm (Settings → Community Nodes) or recreate the two AgenTrux nodes by hand
> (the Code node and wiring are unaffected).

## Templates

### `echo.workflow.json` — Echo bot
`AgenTrux Trigger → Code → AgenTrux Publish`. Reads each event, appends `[n8n echo]` to the
message, and publishes the reply to the same topic. The Code node skips messages that already
contain `[n8n echo]` so a single in/out topic does not loop.

## Other patterns (build from the AgenTrux node)

### Send a file you received from a previous node
`… (binary) → AgenTrux (Upload Payload) → AgenTrux (Publish Event)`
- **Upload Payload**: Input Binary Field = `data` → returns `payload_object_id`.
- **Publish Event**: set *Additional Fields → Payload Object ID* to `={{ $json.payload_object_id }}`
  (object-ref mode; the inline payload is ignored).

### Receive a file and pass it on as a binary
`AgenTrux Trigger → AgenTrux (Download Payload) → … (binary)`
- **Download Payload**: Payload Object ID = `={{ $json.payload_object_id }}` (from the event),
  Output Binary Field = `data`. The next node receives a standard n8n binary file object.

### Process past logs (replay history)
`Manual Trigger → AgenTrux (Read Events) → …`
- **Read Events**: Order = *Oldest First (asc)*, leave **After Cursor** empty to start at the
  oldest retained event. Feed the returned `next.after` back into **After Cursor** to page
  forward through the full history (cursor is the `event_id`).
