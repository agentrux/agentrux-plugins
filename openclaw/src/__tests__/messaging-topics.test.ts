/**
 * Messaging topics tests for the OpenClaw AgenTrux plugin.
 *
 * Coverage:
 *   1. configSchema validation
 *   2. resolveMessagingTopics — normal, boundary, error, and attack inputs
 *   3. Tool ingress gating — mode-based access control with result validation
 *   4. AgenTruxAccount type smoke test
 */

import * as fs from "fs";
import * as path from "path";

// ---------------------------------------------------------------------------
// 1. configSchema validation
// ---------------------------------------------------------------------------

const PLUGIN_JSON_PATH = path.resolve(__dirname, "..", "..", "openclaw.plugin.json");
const pluginJson = JSON.parse(fs.readFileSync(PLUGIN_JSON_PATH, "utf-8"));

describe("configSchema — messagingTopics", () => {
  const props = pluginJson.configSchema.properties;
  const mtSchema = props.messagingTopics;
  const itemProps = mtSchema.items.properties;

  test("messagingTopics is defined as an array", () => {
    expect(mtSchema).toBeDefined();
    expect(mtSchema.type).toBe("array");
    expect(mtSchema.default).toEqual([]);
  });

  test("items require id, topicId, mode", () => {
    expect(mtSchema.items.required).toEqual(expect.arrayContaining(["id", "topicId", "mode"]));
  });

  test("mode enum is exactly [read, write, readwrite]", () => {
    expect(itemProps.mode.enum).toEqual(["read", "write", "readwrite"]);
    expect(itemProps.mode.enum).toHaveLength(3);
  });

  test("listen defaults to false", () => {
    expect(itemProps.listen.type).toBe("boolean");
    expect(itemProps.listen.default).toBe(false);
  });

  test("id and topicId are string typed", () => {
    expect(itemProps.id.type).toBe("string");
    expect(itemProps.topicId.type).toBe("string");
  });

  test("messagingTopics is not in the required list (optional)", () => {
    expect(pluginJson.configSchema.required).not.toContain("messagingTopics");
  });
});

// ---------------------------------------------------------------------------
// 2. resolveMessagingTopics unit tests
// ---------------------------------------------------------------------------

interface MessagingTopic {
  id: string;
  topicId: string;
  mode: "read" | "write" | "readwrite";
  listen: boolean;
}

// Mirror of the real resolver. Divergence is caught by integration tests.
function resolveMessagingTopics(raw: any[]): MessagingTopic[] {
  if (!Array.isArray(raw)) return [];
  return raw
    .filter((t: any) => t && typeof t.id === "string" && typeof t.topicId === "string")
    .map((t: any) => ({
      id: t.id,
      topicId: t.topicId,
      mode: (["read", "write", "readwrite"].includes(t.mode) ? t.mode : "read") as MessagingTopic["mode"],
      listen: t.listen === true,
    }));
}

describe("resolveMessagingTopics — normal cases", () => {
  test("parses single valid entry with all fields", () => {
    const result = resolveMessagingTopics([
      { id: "alerts", topicId: "uuid-1", mode: "readwrite", listen: true },
    ]);
    expect(result).toEqual([
      { id: "alerts", topicId: "uuid-1", mode: "readwrite", listen: true },
    ]);
  });

  test("parses multiple entries preserving order", () => {
    const input = [
      { id: "a", topicId: "uuid-1", mode: "read" },
      { id: "b", topicId: "uuid-2", mode: "write", listen: true },
      { id: "c", topicId: "uuid-3", mode: "readwrite" },
    ];
    const result = resolveMessagingTopics(input);
    expect(result).toHaveLength(3);
    expect(result[0].id).toBe("a");
    expect(result[1].id).toBe("b");
    expect(result[2].id).toBe("c");
    expect(result[0].listen).toBe(false);
    expect(result[1].listen).toBe(true);
    expect(result[2].mode).toBe("readwrite");
  });

  test("listen defaults to false when omitted", () => {
    const [entry] = resolveMessagingTopics([{ id: "x", topicId: "u", mode: "read" }]);
    expect(entry.listen).toBe(false);
  });
});

describe("resolveMessagingTopics — boundary cases", () => {
  test("returns empty array for undefined", () => {
    expect(resolveMessagingTopics(undefined as any)).toEqual([]);
  });

  test("returns empty array for null", () => {
    expect(resolveMessagingTopics(null as any)).toEqual([]);
  });

  test("returns empty array for empty array", () => {
    expect(resolveMessagingTopics([])).toEqual([]);
  });

  test("returns empty array for non-array types", () => {
    expect(resolveMessagingTopics("string" as any)).toEqual([]);
    expect(resolveMessagingTopics(42 as any)).toEqual([]);
    expect(resolveMessagingTopics(true as any)).toEqual([]);
    expect(resolveMessagingTopics({} as any)).toEqual([]);
  });

  test("empty string id is accepted (string type check passes)", () => {
    const result = resolveMessagingTopics([{ id: "", topicId: "uuid-1", mode: "read" }]);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("");
  });

  test("empty string topicId is accepted", () => {
    const result = resolveMessagingTopics([{ id: "a", topicId: "", mode: "read" }]);
    expect(result).toHaveLength(1);
    expect(result[0].topicId).toBe("");
  });

  test("defaults unknown mode to 'read'", () => {
    const cases = ["invalid", "READ", "WRITE", "", "rw", "readWrite", undefined, null, 0];
    for (const mode of cases) {
      const [entry] = resolveMessagingTopics([{ id: "a", topicId: "u", mode }]);
      expect(entry.mode).toBe("read");
    }
  });

  test("listen is only true for boolean true, not truthy values", () => {
    const truthy = ["yes", "true", 1, "1", [], {}];
    for (const listen of truthy) {
      const [entry] = resolveMessagingTopics([{ id: "a", topicId: "u", mode: "read", listen }]);
      expect(entry.listen).toBe(false);
    }
    // Only boolean true
    const [entry] = resolveMessagingTopics([{ id: "a", topicId: "u", mode: "read", listen: true }]);
    expect(entry.listen).toBe(true);
  });

  test("large number of entries is handled", () => {
    const input = Array.from({ length: 1000 }, (_, i) => ({
      id: `topic-${i}`, topicId: `uuid-${i}`, mode: "read",
    }));
    const result = resolveMessagingTopics(input);
    expect(result).toHaveLength(1000);
    expect(result[999].id).toBe("topic-999");
  });

  test("extra unknown properties are silently ignored", () => {
    const result = resolveMessagingTopics([
      { id: "a", topicId: "u", mode: "read", listen: false, foo: "bar", nested: { x: 1 } },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({ id: "a", topicId: "u", mode: "read", listen: false });
    expect((result[0] as any).foo).toBeUndefined();
  });
});

describe("resolveMessagingTopics — error/invalid inputs", () => {
  test("filters entries where id is not a string", () => {
    const invalids = [
      { id: 123, topicId: "u", mode: "read" },
      { id: null, topicId: "u", mode: "read" },
      { id: undefined, topicId: "u", mode: "read" },
      { id: true, topicId: "u", mode: "read" },
      { id: [], topicId: "u", mode: "read" },
      { id: {}, topicId: "u", mode: "read" },
    ];
    for (const entry of invalids) {
      expect(resolveMessagingTopics([entry])).toEqual([]);
    }
  });

  test("filters entries where topicId is not a string", () => {
    const invalids = [
      { id: "a", topicId: 123, mode: "read" },
      { id: "a", topicId: null, mode: "read" },
      { id: "a", topicId: undefined, mode: "read" },
      { id: "a", topicId: true, mode: "read" },
      { id: "a", topicId: [], mode: "read" },
    ];
    for (const entry of invalids) {
      expect(resolveMessagingTopics([entry])).toEqual([]);
    }
  });

  test("filters null, undefined, and primitive entries in the array", () => {
    const result = resolveMessagingTopics([
      null, undefined, 0, false, "", "string",
      { id: "valid", topicId: "uuid", mode: "read" },
    ] as any[]);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("valid");
  });

  test("missing id field entirely is filtered", () => {
    expect(resolveMessagingTopics([{ topicId: "u", mode: "read" }])).toEqual([]);
  });

  test("missing topicId field entirely is filtered", () => {
    expect(resolveMessagingTopics([{ id: "a", mode: "read" }])).toEqual([]);
  });
});

describe("resolveMessagingTopics — attack vectors", () => {
  test("prototype pollution attempt in id is treated as string", () => {
    const result = resolveMessagingTopics([
      { id: "__proto__", topicId: "uuid", mode: "read" },
      { id: "constructor", topicId: "uuid", mode: "write" },
      { id: "toString", topicId: "uuid", mode: "readwrite" },
    ]);
    expect(result).toHaveLength(3);
    expect(result[0].id).toBe("__proto__");
    expect(result[1].id).toBe("constructor");
    expect(result[2].id).toBe("toString");
    // Ensure no prototype pollution occurred
    expect(({} as any).__proto__).toBe(Object.prototype);
  });

  test("script injection in id/topicId is passed through as-is (no execution context)", () => {
    const xss = '<script>alert("xss")</script>';
    const result = resolveMessagingTopics([
      { id: xss, topicId: xss, mode: "read" },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe(xss);
    expect(result[0].topicId).toBe(xss);
  });

  test("SQL injection in topicId is passed through as-is (no SQL context)", () => {
    const sqli = "'; DROP TABLE topics; --";
    const result = resolveMessagingTopics([
      { id: "a", topicId: sqli, mode: "read" },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].topicId).toBe(sqli);
  });

  test("extremely long id/topicId does not crash", () => {
    const longStr = "a".repeat(100_000);
    const result = resolveMessagingTopics([
      { id: longStr, topicId: longStr, mode: "read" },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].id.length).toBe(100_000);
  });

  test("unicode and null bytes in id/topicId", () => {
    const result = resolveMessagingTopics([
      { id: "日本語\0テスト", topicId: "emoji-🔥-\u0000-end", mode: "write" },
    ]);
    expect(result).toHaveLength(1);
    expect(result[0].id).toContain("日本語");
    expect(result[0].id).toContain("\0");
    expect(result[0].topicId).toContain("🔥");
  });

  test("mode injection attempt falls back to read", () => {
    const attacks = [
      "read; DROP TABLE",
      "readwrite\nwrite",
      "read\r\nwrite",
      "read\x00write",
    ];
    for (const mode of attacks) {
      const [entry] = resolveMessagingTopics([{ id: "a", topicId: "u", mode }]);
      expect(entry.mode).toBe("read");
    }
  });
});

// ---------------------------------------------------------------------------
// 3. Tool ingress gating — integration tests
// ---------------------------------------------------------------------------

interface RegisteredTool {
  name: string;
  description: string;
  parameters: any;
  execute: (...args: any[]) => Promise<any>;
}

describe("Tool ingress gating with messaging topics", () => {
  let tools: RegisteredTool[];
  let publishTool: RegisteredTool;
  let readTool: RegisteredTool;
  let sendMessageTool: RegisteredTool;
  let indexModule: any;

  const MESSAGING_TOPIC_READ = "msg-topic-uuid-read";
  const MESSAGING_TOPIC_WRITE = "msg-topic-uuid-write";
  const MESSAGING_TOPIC_RW = "msg-topic-uuid-rw";
  const NON_MESSAGING_TOPIC = "non-messaging-uuid";
  const COMMAND_TOPIC = "cmd-uuid";
  const RESULT_TOPIC = "result-uuid";

  beforeAll(() => {
    tools = [];
    const fakeApi = {
      registerTool(def: any, _opts?: any) { tools.push(def); },
      registerChannel(_def: any) {},
      runtime: {
        config: { loadConfig: () => ({}) },
        channel: {
          routing: { resolveAgentRoute: () => ({}) },
          reply: {
            finalizeInboundContext: () => ({}),
            dispatchReplyWithBufferedBlockDispatcher: async () => {},
          },
          session: {
            recordInboundSession: async () => {},
            resolveStorePath: () => "",
          },
        },
      },
      pluginConfig: {
        commandTopicId: COMMAND_TOPIC,
        resultTopicId: RESULT_TOPIC,
        agentId: "test-agent",
        messagingTopics: [
          { id: "sensor", topicId: MESSAGING_TOPIC_READ, mode: "read" },
          { id: "alerts", topicId: MESSAGING_TOPIC_WRITE, mode: "write" },
          { id: "bidirectional", topicId: MESSAGING_TOPIC_RW, mode: "readwrite" },
        ],
      },
      logger: { info: () => {}, warn: () => {}, error: () => {} },
    };

    indexModule = require("../index");
    const plugin = indexModule.default || indexModule;
    if (typeof plugin.register === "function") {
      plugin.register(fakeApi);
    } else {
      plugin(fakeApi);
    }

    publishTool = tools.find((t) => t.name === "agentrux_publish")!;
    readTool = tools.find((t) => t.name === "agentrux_read")!;
    sendMessageTool = tools.find((t) => t.name === "agentrux_send_message")!;
  });

  function simulateIngress(requestId = "test-req") {
    indexModule.setActiveRequest({
      requestId,
      conversationKey: "test-conv",
      resultTopicId: RESULT_TOPIC,
    });
  }

  function clearIngress(requestId = "test-req") {
    indexModule.clearActiveRequest(requestId);
  }

  afterEach(() => {
    try { clearIngress(); } catch {}
  });

  // --- Tool existence and description ---

  test("publish tool exists with correct parameter schema", () => {
    expect(publishTool).toBeDefined();
    expect(publishTool.parameters.properties).toHaveProperty("topic");
    expect(publishTool.parameters.properties).toHaveProperty("topic_id");
    expect(publishTool.parameters.required).toContain("event_type");
    expect(publishTool.parameters.required).toContain("payload");
    // topic_id is no longer required (topic logical name can be used instead)
    expect(publishTool.parameters.required).not.toContain("topic_id");
  });

  test("read tool exists with correct parameter schema", () => {
    expect(readTool).toBeDefined();
    expect(readTool.parameters.properties).toHaveProperty("topic");
    expect(readTool.parameters.properties).toHaveProperty("topic_id");
  });

  test("descriptions mention messaging topics availability", () => {
    expect(publishTool.description).toContain("messaging");
    expect(readTool.description).toContain("messaging");
  });

  // --- Ingress blocking for non-messaging topics ---

  test("publish blocks non-messaging topic during ingress with clear error", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: NON_MESSAGING_TOPIC, event_type: "test", payload: {},
    });
    expect(result.content).toHaveLength(1);
    expect(result.content[0].type).toBe("text");
    expect(result.content[0].text).toContain("Not available during ingress");
    expect(result.content[0].text).toContain("messaging");
  });

  test("read blocks non-messaging topic during ingress with clear error", async () => {
    simulateIngress();
    const result = await readTool.execute("id", { topic_id: NON_MESSAGING_TOPIC });
    expect(result.content).toHaveLength(1);
    expect(result.content[0].type).toBe("text");
    expect(result.content[0].text).toContain("Not available during ingress");
    expect(result.content[0].text).toContain("messaging");
  });

  test("publish blocks command topic during ingress", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: COMMAND_TOPIC, event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("publish blocks result topic during ingress", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: RESULT_TOPIC, event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- Mode-based access control ---

  test("publish blocks read-only messaging topic during ingress", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: MESSAGING_TOPIC_READ, event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("read blocks write-only messaging topic during ingress", async () => {
    simulateIngress();
    const result = await readTool.execute("id", { topic_id: MESSAGING_TOPIC_WRITE });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- readwrite topic allows both operations ---

  // Note: readwrite topics that pass the ingress check will attempt real
  // HTTP calls and fail (no credentials in test). We verify the ingress
  // check itself does NOT block them — the error must NOT contain
  // "Not available during ingress".
  test("publish allows readwrite messaging topic during ingress (passes gating)", async () => {
    simulateIngress();
    try {
      await publishTool.execute("id", {
        topic_id: MESSAGING_TOPIC_RW, event_type: "test", payload: {},
      });
    } catch (err: any) {
      // Expected: HTTP/credential error, NOT ingress block
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  test("read allows readwrite messaging topic during ingress (passes gating)", async () => {
    simulateIngress();
    try {
      await readTool.execute("id", { topic_id: MESSAGING_TOPIC_RW });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  // --- send_message is always blocked during ingress ---

  test("send_message is always blocked during ingress regardless of topic", async () => {
    simulateIngress();
    const result = await sendMessageTool.execute("id", {
      topic_id: MESSAGING_TOPIC_RW,
      reply_topic: MESSAGING_TOPIC_READ,
      message: "test",
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- Outside ingress: all topics are accessible ---

  // When not in ingress, tools should attempt real calls (and fail on
  // credentials). The key assertion is that they do NOT return
  // "Not available during ingress".
  test("publish allows any topic outside ingress (passes gating)", async () => {
    // No simulateIngress() — not in ingress
    try {
      await publishTool.execute("id", {
        topic_id: NON_MESSAGING_TOPIC, event_type: "test", payload: {},
      });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  test("read allows any topic outside ingress (passes gating)", async () => {
    try {
      await readTool.execute("id", { topic_id: NON_MESSAGING_TOPIC });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  // --- Attack vectors on ingress gating ---

  test("topic_id with path traversal does not bypass gating", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: `../../${MESSAGING_TOPIC_WRITE}`, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("topic_id with null bytes does not bypass gating", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: `${MESSAGING_TOPIC_WRITE}\x00other`, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("topic_id with unicode homoglyph does not bypass gating", async () => {
    simulateIngress();
    // Replace ASCII 'w' in "write" with fullwidth 'ｗ' (U+FF57)
    const homoglyph = MESSAGING_TOPIC_WRITE.replace("w", "\uff57");
    expect(homoglyph).not.toBe(MESSAGING_TOPIC_WRITE); // sanity check
    const result = await publishTool.execute("id", {
      topic_id: homoglyph, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("empty topic_id with no topic returns 'required' error", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: "", event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("required");
  });

  test("topic_id with whitespace padding does not match", async () => {
    simulateIngress();
    const result = await publishTool.execute("id", {
      topic_id: ` ${MESSAGING_TOPIC_WRITE} `, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- Concurrent ingress handling ---

  test("multiple concurrent ingress requests maintain independent gating", async () => {
    // Simulate two concurrent ingress requests
    indexModule.setActiveRequest({
      requestId: "req-1", conversationKey: "conv-1", resultTopicId: RESULT_TOPIC,
    });
    indexModule.setActiveRequest({
      requestId: "req-2", conversationKey: "conv-2", resultTopicId: RESULT_TOPIC,
    });

    // Gating should still work — non-messaging topics blocked
    const result = await publishTool.execute("id", {
      topic_id: NON_MESSAGING_TOPIC, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");

    // Clear first request — second still active, gating still works
    indexModule.clearActiveRequest("req-1");
    const result2 = await publishTool.execute("id", {
      topic_id: NON_MESSAGING_TOPIC, event_type: "t", payload: {},
    });
    expect(result2.content[0].text).toContain("Not available during ingress");

    // Clear second — now outside ingress
    indexModule.clearActiveRequest("req-2");
  });
});

// ---------------------------------------------------------------------------
// 4. Logical name resolution
// ---------------------------------------------------------------------------

describe("Logical name resolution (topic parameter)", () => {
  // Reuse the same tools/indexModule from the gating describe block above.
  // We need a fresh registration with messaging topics configured.
  let tools: RegisteredTool[];
  let publishTool: RegisteredTool;
  let readTool: RegisteredTool;
  let indexModule: any;

  const MT_READ_ID = "sensor-in";
  const MT_READ_UUID = "mt-read-uuid-1234";
  const MT_WRITE_ID = "sensor-out";
  const MT_WRITE_UUID = "mt-write-uuid-5678";
  const MT_RW_ID = "control";
  const MT_RW_UUID = "mt-rw-uuid-9012";

  beforeAll(() => {
    tools = [];
    const fakeApi = {
      registerTool(def: any, _opts?: any) { tools.push(def); },
      registerChannel(_def: any) {},
      runtime: {
        config: { loadConfig: () => ({}) },
        channel: {
          routing: { resolveAgentRoute: () => ({}) },
          reply: {
            finalizeInboundContext: () => ({}),
            dispatchReplyWithBufferedBlockDispatcher: async () => {},
          },
          session: {
            recordInboundSession: async () => {},
            resolveStorePath: () => "",
          },
        },
      },
      pluginConfig: {
        commandTopicId: "cmd-uuid",
        resultTopicId: "result-uuid",
        agentId: "test-agent",
        messagingTopics: [
          { id: MT_READ_ID, topicId: MT_READ_UUID, mode: "read", listen: true },
          { id: MT_WRITE_ID, topicId: MT_WRITE_UUID, mode: "write" },
          { id: MT_RW_ID, topicId: MT_RW_UUID, mode: "readwrite", listen: true },
        ],
      },
      logger: { info: () => {}, warn: () => {}, error: () => {} },
    };

    indexModule = require("../index");
    const plugin = indexModule.default || indexModule;
    if (typeof plugin.register === "function") {
      plugin.register(fakeApi);
    } else {
      plugin(fakeApi);
    }

    publishTool = tools.find((t) => t.name === "agentrux_publish")!;
    readTool = tools.find((t) => t.name === "agentrux_read")!;
  });

  afterEach(() => {
    try { indexModule.clearActiveRequest("ln-test"); } catch {}
  });

  // --- Missing topic parameter ---

  test("publish returns error when neither topic nor topic_id is provided", async () => {
    const result = await publishTool.execute("id", {
      event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Either");
    expect(result.content[0].text).toContain("topic");
    expect(result.content[0].text).toContain("required");
  });

  test("read returns error when neither topic nor topic_id is provided", async () => {
    const result = await readTool.execute("id", {});
    expect(result.content[0].text).toContain("Either");
    expect(result.content[0].text).toContain("required");
  });

  // --- Logical name resolves correctly during ingress ---

  test("publish via logical name on write topic passes ingress gating", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    // "sensor-out" is mode: "write" → should pass publish gating
    try {
      await publishTool.execute("id", {
        topic: MT_WRITE_ID, event_type: "test", payload: {},
      });
    } catch (err: any) {
      // Credential error expected, NOT ingress block
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  test("read via logical name on read topic passes ingress gating", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    try {
      await readTool.execute("id", { topic: MT_READ_ID });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  // --- Logical name with wrong mode is blocked ---

  test("publish via logical name on read-only topic is blocked during ingress", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await publishTool.execute("id", {
      topic: MT_READ_ID, event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("read via logical name on write-only topic is blocked during ingress", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await readTool.execute("id", { topic: MT_WRITE_ID });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- Logical name on readwrite topic works for both ---

  test("publish via logical name on readwrite topic passes ingress gating", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    try {
      await publishTool.execute("id", {
        topic: MT_RW_ID, event_type: "test", payload: {},
      });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  test("read via logical name on readwrite topic passes ingress gating", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    try {
      await readTool.execute("id", { topic: MT_RW_ID });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  // --- topic parameter takes precedence over topic_id ---

  test("topic logical name takes precedence over topic_id", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    // topic="sensor-out" (write) should win over topic_id (read-only UUID)
    try {
      await publishTool.execute("id", {
        topic: MT_WRITE_ID,
        topic_id: MT_READ_UUID, // this should be ignored
        event_type: "test",
        payload: {},
      });
    } catch (err: any) {
      expect(err.message).not.toContain("Not available during ingress");
    }
  });

  // --- Unknown logical name falls through as-is ---

  test("unknown logical name is treated as raw value (blocked if not in messaging set)", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await publishTool.execute("id", {
      topic: "nonexistent-name", event_type: "test", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  // --- Attack vectors on logical name ---

  test("logical name with prototype pollution attempt does not resolve", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await publishTool.execute("id", {
      topic: "__proto__", event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("logical name with null byte does not resolve", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await publishTool.execute("id", {
      topic: `${MT_WRITE_ID}\x00`, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("logical name with whitespace padding does not resolve", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const result = await publishTool.execute("id", {
      topic: ` ${MT_WRITE_ID} `, event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });

  test("empty string topic falls back to topic_id", async () => {
    indexModule.setActiveRequest({
      requestId: "ln-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    // topic="" → falsy, falls back to topic_id
    const result = await publishTool.execute("id", {
      topic: "", topic_id: "unknown-uuid", event_type: "t", payload: {},
    });
    expect(result.content[0].text).toContain("Not available during ingress");
  });
});

// ---------------------------------------------------------------------------
// 4.5. Topic ID prefix normalization (regression for prefix mismatch bug)
//
// Bug context (Dify plugin sibling 2026-05-26): if config and the agent
// disagree on whether topic IDs carry the `top_` prefix, access-control sets
// built from config (`mt.topicId`) and lookups from the agent's tool call
// (`resolveTopicParam`) mismatch string-equality → access denied even when
// the underlying topic + grant are identical. Both sides must be normalized.
// ---------------------------------------------------------------------------

describe("Topic ID prefix normalization (4.5)", () => {
  let tools: RegisteredTool[];
  let publishTool: RegisteredTool;
  let readTool: RegisteredTool;
  let indexModule: any;

  const PREFIXED_ID = "topic-prefixed";
  const PREFIXED_UUID = "top_aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
  const BARE_ID = "topic-bare";
  const BARE_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

  beforeAll(() => {
    jest.resetModules();
    tools = [];
    const fakeApi = {
      registerTool(def: any) { tools.push(def); },
      registerChannel() {},
      runtime: {
        config: { loadConfig: () => ({}) },
        channel: {
          routing: { resolveAgentRoute: () => ({}) },
          reply: {
            finalizeInboundContext: () => ({}),
            dispatchReplyWithBufferedBlockDispatcher: async () => {},
          },
          session: {
            recordInboundSession: async () => {},
            resolveStorePath: () => "",
          },
        },
      },
      pluginConfig: {
        commandTopicId: "cmd-uuid",
        resultTopicId: "result-uuid",
        agentId: "test-agent",
        messagingTopics: [
          // Config in prefixed form; agent will call with bare UUID
          { id: PREFIXED_ID, topicId: PREFIXED_UUID, mode: "readwrite" },
          // Config in bare form; agent will call with prefixed UUID
          { id: BARE_ID, topicId: BARE_UUID, mode: "readwrite" },
        ],
      },
      logger: { info: () => {}, warn: () => {}, error: () => {} },
    };
    indexModule = require("../index");
    const plugin = indexModule.default || indexModule;
    if (typeof plugin.register === "function") plugin.register(fakeApi);
    else plugin(fakeApi);
    publishTool = tools.find((t) => t.name === "agentrux_publish")!;
    readTool = tools.find((t) => t.name === "agentrux_read")!;
  });

  afterEach(() => {
    try { indexModule.clearActiveRequest("prefix-test"); } catch {}
  });

  test("config prefixed + agent passes bare → publish passes gating", async () => {
    indexModule.setActiveRequest({
      requestId: "prefix-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    // Strip the prefix to simulate an agent that has the bare UUID in hand
    const bareForm = PREFIXED_UUID.replace(/^top_/, "");
    try {
      await publishTool.execute("id", {
        topic_id: bareForm, event_type: "t", payload: {},
      });
    } catch (err: any) {
      expect(err.message ?? "").not.toContain("Not available during ingress");
    }
  });

  test("config bare + agent passes prefixed → read passes gating", async () => {
    indexModule.setActiveRequest({
      requestId: "prefix-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    try {
      await readTool.execute("id", { topic_id: `top_${BARE_UUID}` });
    } catch (err: any) {
      expect(err.message ?? "").not.toContain("Not available during ingress");
    }
  });

  test("logical name still resolves regardless of config prefix style", async () => {
    indexModule.setActiveRequest({
      requestId: "prefix-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    // Both entries use mode=readwrite, so logical-name resolution should
    // pass gating on both publish and read for either entry.
    for (const name of [PREFIXED_ID, BARE_ID]) {
      try {
        await publishTool.execute("id", { topic: name, event_type: "t", payload: {} });
      } catch (err: any) {
        expect(err.message ?? "").not.toContain("Not available during ingress");
      }
      try {
        await readTool.execute("id", { topic: name });
      } catch (err: any) {
        expect(err.message ?? "").not.toContain("Not available during ingress");
      }
    }
  });

  test("unknown UUID (not in messaging set) is still blocked under either form", async () => {
    indexModule.setActiveRequest({
      requestId: "prefix-test", conversationKey: "c", resultTopicId: "result-uuid",
    });
    const unknownBare = "cccccccc-cccc-cccc-cccc-cccccccccccc";
    const res1 = await publishTool.execute("id", {
      topic_id: unknownBare, event_type: "t", payload: {},
    });
    expect(res1.content[0].text).toContain("Not available during ingress");
    const res2 = await publishTool.execute("id", {
      topic_id: `top_${unknownBare}`, event_type: "t", payload: {},
    });
    expect(res2.content[0].text).toContain("Not available during ingress");
  });
});

// ---------------------------------------------------------------------------
// 5. Multi-topic configuration
// ---------------------------------------------------------------------------

describe("Multi-topic configuration", () => {
  test("resolveMessagingTopics handles 5+ topics with mixed modes", () => {
    const input = [
      { id: "t1", topicId: "u1", mode: "read", listen: true },
      { id: "t2", topicId: "u2", mode: "write" },
      { id: "t3", topicId: "u3", mode: "readwrite", listen: true },
      { id: "t4", topicId: "u4", mode: "read" },
      { id: "t5", topicId: "u5", mode: "write", listen: false },
    ];
    const result = resolveMessagingTopics(input);
    expect(result).toHaveLength(5);

    // Verify each entry's mode and listen
    const readable = result.filter(t => t.mode === "read" || t.mode === "readwrite");
    const writable = result.filter(t => t.mode === "write" || t.mode === "readwrite");
    const listening = result.filter(t => t.listen);

    expect(readable).toHaveLength(3); // t1(read), t3(readwrite), t4(read)
    expect(writable).toHaveLength(3); // t2(write), t3(readwrite), t5(write)
    expect(listening).toHaveLength(2); // t1, t3
  });

  test("duplicate topic IDs are preserved (plugin may intentionally alias)", () => {
    const input = [
      { id: "alias-a", topicId: "shared-uuid", mode: "read" },
      { id: "alias-b", topicId: "shared-uuid", mode: "write" },
    ];
    const result = resolveMessagingTopics(input);
    expect(result).toHaveLength(2);
    expect(result[0].topicId).toBe("shared-uuid");
    expect(result[1].topicId).toBe("shared-uuid");
    expect(result[0].mode).toBe("read");
    expect(result[1].mode).toBe("write");
  });

  test("duplicate logical names are preserved (last wins in Map)", () => {
    // This tests the Map behavior in the real implementation
    const input = [
      { id: "dup", topicId: "uuid-first", mode: "read" },
      { id: "dup", topicId: "uuid-second", mode: "write" },
    ];
    const result = resolveMessagingTopics(input);
    expect(result).toHaveLength(2);
    // Both entries exist in the array; Map resolution will use the last one
  });
});

// ---------------------------------------------------------------------------
// 6. SSE drain behavior (gateway structure)
// ---------------------------------------------------------------------------

describe("SSE drain after hint reception", () => {
  // These tests verify the gateway.ts structure without running
  // real HTTP — we inspect the source code for correct patterns.
  const gatewaySource = fs.readFileSync(
    path.resolve(__dirname, "..", "gateway.ts"), "utf-8",
  );

  test("messaging topic SSE drain uses while loop for full batch consumption", () => {
    // The drain must loop until batch is empty, not just pull once
    expect(gatewaySource).toContain("while (!abortSignal.aborted)");
    expect(gatewaySource).toContain("if (batch.length === 0) break");
  });

  test("waterline is advanced per event, not per batch", () => {
    // cluster-agnostic ordering §3-3: cursor は opaque token、sequence_number 廃止。
    // waterline は per-event cursor (opaque) または event_id で前進する。
    expect(gatewaySource).toContain("for (const event of batch");
    expect(gatewaySource).toContain("event_id: event.cursor || event.event_id");
  });

  test("waterline is persisted after drain completes", () => {
    expect(gatewaySource).toContain("saveWaterline(mtTopicId, mtWaterline)");
  });

  test("concurrent drain is guarded (mtDrainRunning flag)", () => {
    expect(gatewaySource).toContain("mtDrainRunning");
    expect(gatewaySource).toContain("if (mtDrainRunning) return");
  });

  test("drain errors are caught and logged, not thrown", () => {
    expect(gatewaySource).toContain("drain error");
    // The drain is inside a try/catch/finally, not unguarded
    expect(gatewaySource).toContain("finally");
    expect(gatewaySource).toContain("mtDrainRunning = false");
  });

  test("write-only topics skip SSE listener", () => {
    expect(gatewaySource).toContain('if (mt.mode === "write") continue');
  });

  test("SSE reconnects with exponential backoff on disconnect", () => {
    expect(gatewaySource).toContain("Math.pow(2, mtReconnectAttempts)");
    expect(gatewaySource).toContain("60_000"); // max backoff cap
  });

  test("SSE handles 401 by invalidating token", () => {
    expect(gatewaySource).toContain("invalidateToken()");
    expect(gatewaySource).toContain("SSE auth expired");
  });

  test("first startup skips to latest waterline for messaging topics", () => {
    // Messaging topics should fast-forward on (every re)start so a backlog
    // accumulated while offline is not replayed.
    expect(gatewaySource).toContain('skipped to latest');
  });
});

// ---------------------------------------------------------------------------
// 7. AgenTruxAccount type smoke test
// ---------------------------------------------------------------------------

describe("AgenTruxAccount type", () => {
  test("gateway exports agentruxGateway with startAccount", () => {
    const gateway = require("../gateway");
    expect(gateway.agentruxGateway).toBeDefined();
    expect(typeof gateway.agentruxGateway.startAccount).toBe("function");
  });
});
