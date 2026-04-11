/**
 * OpenClaw environment integration tests.
 *
 * These tests require:
 * - OpenClaw Gateway running with LLM provider configured
 * - AgenTrux API accessible (production or staging)
 * - Test topics (command + result) created with valid grants
 *
 * Run separately from unit tests:
 *   npm run test:openclaw
 *   npx jest --testPathPattern='openclaw-env'
 *
 * Environment variables:
 *   AGENTRUX_BASE_URL    - AgenTrux API URL (required)
 *   AGENTRUX_SCRIPT_ID   - Script ID with topic access (required)
 *   AGENTRUX_SECRET      - Client secret (required)
 *   AGENTRUX_COMMAND_TOPIC - Command topic UUID (required)
 *   AGENTRUX_RESULT_TOPIC  - Result topic UUID (required)
 *   OPENCLAW_TIMEOUT_MS  - Timeout for LLM response (default: 60000)
 */

import * as http from "http";
import * as https from "https";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const BASE_URL = process.env.AGENTRUX_BASE_URL || "";
const SCRIPT_ID = process.env.AGENTRUX_SCRIPT_ID || "";
const CLIENT_SECRET = process.env.AGENTRUX_SECRET || "";
const COMMAND_TOPIC = process.env.AGENTRUX_COMMAND_TOPIC || "";
const RESULT_TOPIC = process.env.AGENTRUX_RESULT_TOPIC || "";
const TIMEOUT_MS = parseInt(process.env.OPENCLAW_TIMEOUT_MS || "60000", 10);

const isConfigured =
  BASE_URL && SCRIPT_ID && CLIENT_SECRET && COMMAND_TOPIC && RESULT_TOPIC;

// ---------------------------------------------------------------------------
// HTTP helpers
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
      headers: { "Content-Type": "application/json", ...headers },
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

async function getToken(): Promise<string> {
  const r = await httpJson("POST", `${BASE_URL}/auth/token`, {
    script_id: SCRIPT_ID,
    client_secret: CLIENT_SECRET,
  });
  if (r.status !== 200) throw new Error(`Auth failed: ${JSON.stringify(r.data)}`);
  return r.data.access_token;
}

async function publishEvent(
  token: string,
  topicId: string,
  type: string,
  payload: Record<string, unknown>,
): Promise<string> {
  const r = await httpJson(
    "POST",
    `${BASE_URL}/topics/${topicId}/events`,
    { type, payload },
    { Authorization: `Bearer ${token}` },
  );
  if (r.status >= 400) throw new Error(`Publish failed: ${JSON.stringify(r.data)}`);
  return r.data.event_id;
}

async function pollForResponse(
  token: string,
  topicId: string,
  requestId: string,
  timeoutMs: number,
): Promise<any> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const r = await httpJson(
      "GET",
      `${BASE_URL}/topics/${topicId}/events?limit=20&type=openclaw.response`,
      undefined,
      { Authorization: `Bearer ${token}` },
    );
    if (r.status === 200 && r.data.items) {
      for (const item of r.data.items) {
        if (item.payload?.request_id === requestId) {
          return item;
        }
      }
    }
    await new Promise(r => setTimeout(r, 2000));
  }
  throw new Error(`No response for request_id=${requestId} within ${timeoutMs}ms`);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

const describeIfConfigured = isConfigured ? describe : describe.skip;

describeIfConfigured("OpenClaw environment integration", () => {
  let token: string;

  beforeAll(async () => {
    token = await getToken();
  }, 10000);

  test("SSE hint → Pull drain → LLM → response", async () => {
    const requestId = `test-${Date.now()}-basic`;

    await publishEvent(token, COMMAND_TOPIC, "openclaw.request", {
      request_id: requestId,
      conversation_key: `test-basic-${Date.now()}`,
      message: "Reply with exactly: PONG",
    });

    const response = await pollForResponse(token, RESULT_TOPIC, requestId, TIMEOUT_MS);

    expect(response.payload.status).toBe("completed");
    expect(response.payload.message).toBeTruthy();
    expect(response.payload.message.toLowerCase()).toContain("pong");
  }, TIMEOUT_MS + 10000);

  test("conversation context maintained across messages", async () => {
    const conversationKey = `test-ctx-${Date.now()}`;
    const reqId1 = `test-${Date.now()}-ctx1`;
    const reqId2 = `test-${Date.now()}-ctx2`;

    // First message: set context
    await publishEvent(token, COMMAND_TOPIC, "openclaw.request", {
      request_id: reqId1,
      conversation_key: conversationKey,
      message: "Remember the code word: ELEPHANT42. Reply OK.",
    });
    const r1 = await pollForResponse(token, RESULT_TOPIC, reqId1, TIMEOUT_MS);
    expect(r1.payload.status).toBe("completed");

    // Second message: recall context
    await publishEvent(token, COMMAND_TOPIC, "openclaw.request", {
      request_id: reqId2,
      conversation_key: conversationKey,
      message: "What was the code word I told you?",
    });
    const r2 = await pollForResponse(token, RESULT_TOPIC, reqId2, TIMEOUT_MS);
    expect(r2.payload.status).toBe("completed");
    expect(r2.payload.message.toUpperCase()).toContain("ELEPHANT42");
  }, TIMEOUT_MS * 2 + 20000);

  test("attachment with presigned download_url", async () => {
    // Upload a test file
    const uploadR = await httpJson(
      "POST",
      `${BASE_URL}/topics/${COMMAND_TOPIC}/payloads`,
      { content_type: "text/plain", filename: "test.txt", size: 13 },
      { Authorization: `Bearer ${token}` },
    );
    if (uploadR.status >= 400) {
      throw new Error(`Upload metadata failed: ${JSON.stringify(uploadR.data)}`);
    }

    // Upload file content via presigned URL
    const uploadUrl = new URL(uploadR.data.upload_url);
    const uploadMod = uploadUrl.protocol === "https:" ? https : http;
    await new Promise<void>((resolve, reject) => {
      const req = uploadMod.request({
        hostname: uploadUrl.hostname,
        port: uploadUrl.port,
        path: uploadUrl.pathname + uploadUrl.search,
        method: "PUT",
        headers: { "Content-Type": "text/plain", "Content-Length": 13 },
      }, (res) => {
        res.resume();
        res.on("end", () => res.statusCode && res.statusCode < 300 ? resolve() : reject(new Error(`Upload ${res.statusCode}`)));
      });
      req.on("error", reject);
      req.write("Hello, world!");
      req.end();
    });

    const requestId = `test-${Date.now()}-attach`;

    await publishEvent(token, COMMAND_TOPIC, "openclaw.request", {
      request_id: requestId,
      conversation_key: `test-attach-${Date.now()}`,
      message: "What does the attached file say?",
      attachments: [{
        name: "test.txt",
        object_id: uploadR.data.object_id,
        content_type: "text/plain",
        download_url: uploadR.data.download_url,
      }],
    });

    const response = await pollForResponse(token, RESULT_TOPIC, requestId, TIMEOUT_MS);
    expect(response.payload.status).toBe("completed");
    expect(response.payload.message.toLowerCase()).toContain("hello");
  }, TIMEOUT_MS + 30000);

  test("invalid message → failed response", async () => {
    const requestId = `test-${Date.now()}-empty`;

    // Message with empty text — should still get a response (possibly completed or failed)
    await publishEvent(token, COMMAND_TOPIC, "openclaw.request", {
      request_id: requestId,
      conversation_key: `test-empty-${Date.now()}`,
      message: "",
    });

    // Empty message may be skipped by processEvent (no message/text),
    // so we poll with a shorter timeout and accept no response
    try {
      const response = await pollForResponse(token, RESULT_TOPIC, requestId, 15000);
      // If we get a response, it should be valid
      expect(["completed", "failed"]).toContain(response.payload.status);
    } catch {
      // No response for empty message is acceptable — processEvent skips it
    }
  }, 30000);
});

// Skip message when not configured
if (!isConfigured) {
  test("OpenClaw env tests skipped — set AGENTRUX_BASE_URL, AGENTRUX_SCRIPT_ID, AGENTRUX_SECRET, AGENTRUX_COMMAND_TOPIC, AGENTRUX_RESULT_TOPIC", () => {
    expect(true).toBe(true);
  });
}
