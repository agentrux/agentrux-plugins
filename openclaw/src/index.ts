/**
 * AgenTrux plugin for OpenClaw — Agent-to-Agent authenticated Pub/Sub.
 *
 * Provides tools for:
 *   - activate: Exchange activation token for permanent credentials
 *   - publish: Send events to a topic
 *   - read: Read events from a topic
 *   - send_message: Send a message to another agent (request-response pattern)
 *   - redeem_grant: Redeem a grant token for cross-account access
 *
 * Credentials are persisted to ~/.agentrux/credentials.json (0600).
 * JWT is auto-refreshed before expiry.
 */

import * as fs from "fs";
import * as path from "path";
import * as https from "https";
import * as http from "http";

// ---------------------------------------------------------------------------
// Config & State
// ---------------------------------------------------------------------------

const CREDENTIALS_PATH = path.join(
  process.env.HOME || "~",
  ".agentrux",
  "credentials.json",
);

interface Credentials {
  base_url: string;
  script_id: string;
  secret: string;
}

interface TokenState {
  access_token: string;
  refresh_token: string;
  expires_at: number; // epoch ms
}

let credentials: Credentials | null = null;
let tokenState: TokenState | null = null;

// ---------------------------------------------------------------------------
// HTTP helper (no external deps)
// ---------------------------------------------------------------------------

function httpJson(
  method: string,
  url: string,
  body?: Record<string, unknown>,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const opts = {
      method,
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
    };
    const req = mod.request(opts, (res) => {
      let raw = "";
      res.on("data", (c: Buffer) => (raw += c.toString()));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode || 0, data: JSON.parse(raw) });
        } catch {
          resolve({ status: res.statusCode || 0, data: raw });
        }
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Credential management
// ---------------------------------------------------------------------------

function loadCredentials(): Credentials | null {
  try {
    if (fs.existsSync(CREDENTIALS_PATH)) {
      return JSON.parse(fs.readFileSync(CREDENTIALS_PATH, "utf-8"));
    }
  } catch {}
  return null;
}

function saveCredentials(creds: Credentials): void {
  const dir = path.dirname(CREDENTIALS_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.writeFileSync(CREDENTIALS_PATH, JSON.stringify(creds, null, 2), {
    mode: 0o600,
  });
  credentials = creds;
}

async function ensureToken(): Promise<string> {
  if (!credentials) {
    credentials = loadCredentials();
    if (!credentials) throw new Error("Not connected to AgenTrux. Use activate first.");
  }

  // Valid token with 60s buffer
  if (tokenState && tokenState.expires_at > Date.now() + 60_000) {
    return tokenState.access_token;
  }

  // Try refresh
  if (tokenState?.refresh_token) {
    const r = await httpJson("POST", `${credentials.base_url}/auth/refresh`, {
      refresh_token: tokenState.refresh_token,
    });
    if (r.status === 200) {
      tokenState = {
        access_token: r.data.access_token,
        refresh_token: r.data.refresh_token,
        expires_at: new Date(r.data.expires_at).getTime(),
      };
      return tokenState.access_token;
    }
  }

  // Full auth
  const r = await httpJson("POST", `${credentials.base_url}/auth/token`, {
    script_id: credentials.script_id,
    secret: credentials.secret,
  });
  if (r.status !== 200) throw new Error(`Auth failed: ${JSON.stringify(r.data)}`);
  tokenState = {
    access_token: r.data.access_token,
    refresh_token: r.data.refresh_token,
    expires_at: new Date(r.data.expires_at).getTime(),
  };
  return tokenState.access_token;
}

async function authRequest(
  method: string,
  path: string,
  body?: Record<string, unknown>,
): Promise<any> {
  const token = await ensureToken();
  const r = await httpJson(method, `${credentials!.base_url}${path}`, body, {
    Authorization: `Bearer ${token}`,
  });
  if (r.status === 401) {
    // Token expired — retry once
    tokenState = null;
    const newToken = await ensureToken();
    const retry = await httpJson(method, `${credentials!.base_url}${path}`, body, {
      Authorization: `Bearer ${newToken}`,
    });
    if (retry.status >= 400) throw new Error(`Request failed: ${JSON.stringify(retry.data)}`);
    return retry.data;
  }
  if (r.status >= 400) throw new Error(`Request failed (${r.status}): ${JSON.stringify(r.data)}`);
  return r.data;
}

// ---------------------------------------------------------------------------
// Plugin registration
// ---------------------------------------------------------------------------

export default function (api: any) {
  // --- activate ---
  api.registerTool(
    {
      name: "agentrux_activate",
      description:
        "Connect to AgenTrux with a one-time activation token. " +
        "Returns script_id, secret, and available topics. " +
        "Credentials are saved permanently for future sessions.",
      parameters: {
        type: "object",
        properties: {
          token: {
            type: "string",
            description: "One-time activation token (atk_...)",
          },
          base_url: {
            type: "string",
            description: "AgenTrux API URL (default: https://api.agentrux.example.com)",
          },
        },
        required: ["token"],
      },
      async execute(_id: string, params: { token: string; base_url?: string }) {
        const baseUrl = params.base_url || "https://api.agentrux.example.com";
        const r = await httpJson("POST", `${baseUrl}/auth/activate`, {
          token: params.token,
        });
        if (r.status !== 200) {
          return { content: [{ type: "text", text: `Activation failed: ${JSON.stringify(r.data)}` }] };
        }
        saveCredentials({
          base_url: baseUrl,
          script_id: r.data.script_id,
          secret: r.data.secret,
        });
        const grants = (r.data.grants || [])
          .map((g: any) => `  - ${g.topic_id} (${g.action})`)
          .join("\n");
        return {
          content: [{
            type: "text",
            text: `Connected to AgenTrux!\n` +
              `Script ID: ${r.data.script_id}\n` +
              `Available topics:\n${grants}\n` +
              `Credentials saved to ${CREDENTIALS_PATH}`,
          }],
        };
      },
    },
    { optional: true },
  );

  // --- publish ---
  api.registerTool({
    name: "agentrux_publish",
    description:
      "Publish an event to an AgenTrux topic. " +
      "Use this to send data or messages to other agents.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "UUID of the topic" },
        event_type: { type: "string", description: "Event type (e.g. 'message.send')" },
        payload: { type: "object", description: "JSON payload" },
        correlation_id: { type: "string", description: "Optional correlation ID for request-response" },
        reply_topic: { type: "string", description: "Optional topic UUID for replies" },
      },
      required: ["topic_id", "event_type", "payload"],
    },
    async execute(_id: string, params: any) {
      const body: any = { type: params.event_type, payload: params.payload };
      if (params.correlation_id) body.correlation_id = params.correlation_id;
      if (params.reply_topic) body.reply_topic = params.reply_topic;

      const result = await authRequest("POST", `/topics/${params.topic_id}/events`, body);
      return {
        content: [{
          type: "text",
          text: `Event published (event_id: ${result.event_id})`,
        }],
      };
    },
  });

  // --- read ---
  api.registerTool({
    name: "agentrux_read",
    description:
      "Read events from an AgenTrux topic. " +
      "Returns the latest events with optional type filter.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "UUID of the topic" },
        limit: { type: "number", description: "Max events to return (default 10)" },
        event_type: { type: "string", description: "Filter by event type" },
      },
      required: ["topic_id"],
    },
    async execute(_id: string, params: any) {
      const query = new URLSearchParams();
      query.set("limit", String(params.limit || 10));
      if (params.event_type) query.set("type", params.event_type);

      const result = await authRequest("GET", `/topics/${params.topic_id}/events?${query}`);
      const items = result.items || [];
      if (items.length === 0) {
        return { content: [{ type: "text", text: "No events found." }] };
      }
      const lines = items.map((e: any) =>
        `[seq:${e.sequence_no}] ${e.type} — ${JSON.stringify(e.payload)} (${e.correlation_id || "no correlation"})`,
      );
      return {
        content: [{ type: "text", text: `${items.length} events:\n${lines.join("\n")}` }],
      };
    },
  });

  // --- send_message (request-response shorthand) ---
  api.registerTool({
    name: "agentrux_send_message",
    description:
      "Send a message to another agent and wait for a reply. " +
      "Uses correlation_id + reply_topic for request-response pattern.",
    parameters: {
      type: "object",
      properties: {
        topic_id: { type: "string", description: "Target agent's topic UUID" },
        reply_topic: { type: "string", description: "Your topic UUID for receiving replies" },
        message: { type: "string", description: "Message text to send" },
        timeout_seconds: { type: "number", description: "How long to wait for reply (default 30)" },
      },
      required: ["topic_id", "reply_topic", "message"],
    },
    async execute(_id: string, params: any) {
      const corrId = `msg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const timeout = (params.timeout_seconds || 30) * 1000;

      // Publish request
      await authRequest("POST", `/topics/${params.topic_id}/events`, {
        type: "message.request",
        payload: { text: params.message },
        correlation_id: corrId,
        reply_topic: params.reply_topic,
      });

      // Poll for reply
      const start = Date.now();
      while (Date.now() - start < timeout) {
        const result = await authRequest("GET", `/topics/${params.reply_topic}/events?limit=20`);
        for (const e of result.items || []) {
          if (e.correlation_id === corrId) {
            return {
              content: [{
                type: "text",
                text: `Reply received:\n${JSON.stringify(e.payload, null, 2)}`,
              }],
            };
          }
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      return { content: [{ type: "text", text: `No reply within ${params.timeout_seconds || 30}s.` }] };
    },
  });

  // --- redeem_grant ---
  api.registerTool(
    {
      name: "agentrux_redeem_grant",
      description:
        "Redeem a grant token to gain access to another account's topic. " +
        "After redemption, you can publish/read on the granted topic.",
      parameters: {
        type: "object",
        properties: {
          token: { type: "string", description: "Grant token (gtk_...)" },
        },
        required: ["token"],
      },
      async execute(_id: string, params: { token: string }) {
        if (!credentials) {
          credentials = loadCredentials();
          if (!credentials) throw new Error("Not connected. Use activate first.");
        }
        const r = await httpJson("POST", `${credentials.base_url}/auth/redeem-grant`, {
          token: params.token,
          script_id: credentials.script_id,
          secret: credentials.secret,
        });
        if (r.status >= 400) {
          return { content: [{ type: "text", text: `Grant redemption failed: ${JSON.stringify(r.data)}` }] };
        }
        // Invalidate token cache so next request picks up new scope
        tokenState = null;
        return {
          content: [{
            type: "text",
            text: `Access granted!\n` +
              `Topic: ${r.data.topic_id} (${r.data.action})\n` +
              `Grant ID: ${r.data.grant_id}`,
          }],
        };
      },
    },
    { optional: true },
  );
}
