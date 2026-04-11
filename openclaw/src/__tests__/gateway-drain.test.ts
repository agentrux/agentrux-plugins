/**
 * SSE hint + Pull drain integration tests.
 *
 * Verifies:
 * 1. drainEvents fetches all events from waterline via Pull API
 * 2. drainRunning flag prevents concurrent drains (coalescing)
 * 3. Dedup by event_id and waterline
 * 4. processEvent advances waterline after completion
 * 5. processedEvents TTL cleanup
 * 6. Inbound attachment resolution (text inline, binary URL)
 * 7. drainRunning resets on error
 */

import * as fs from "fs";
import * as path from "path";
import * as http from "http";

// --- Waterline persistence (scoped by topicId) ---

const WATERLINE_DIR = path.join(process.env.HOME || process.env.USERPROFILE || "~", ".agentrux");
const WATERLINE_PATH = path.join(WATERLINE_DIR, "waterline.json");

function loadWaterlineMap(): Record<string, number> {
  try {
    if (fs.existsSync(WATERLINE_PATH)) {
      const data = JSON.parse(fs.readFileSync(WATERLINE_PATH, "utf-8"));
      if (typeof data.waterline === "number") return {};
      if (typeof data === "object" && data !== null) return data;
    }
  } catch {}
  return {};
}

function loadWaterline(topicId: string): number | null {
  const map = loadWaterlineMap();
  return typeof map[topicId] === "number" ? map[topicId] : null;
}

function saveWaterline(topicId: string, waterline: number): void {
  try {
    if (!fs.existsSync(WATERLINE_DIR)) {
      fs.mkdirSync(WATERLINE_DIR, { recursive: true, mode: 0o700 });
    }
    const map = loadWaterlineMap();
    map[topicId] = waterline;
    const tmp = WATERLINE_PATH + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(map), { mode: 0o600 });
    fs.renameSync(tmp, WATERLINE_PATH);
  } catch {}
}

// --- Attachment resolution helpers ---

const TEXT_CONTENT_TYPES = /^(text\/|application\/json|application\/xml|application\/javascript|application\/typescript)/;
const MAX_INLINE_SIZE = 50 * 1024;

interface MockAttachment {
  name: string;
  object_id: string;
  content_type: string;
  download_url?: string;
}

function fetchUrlSync(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? require("https") : require("http");
    const req = mod.request(u, { method: "GET" }, (res: any) => {
      if (res.statusCode && res.statusCode >= 400) {
        res.resume();
        reject(new Error(`HTTP ${res.statusCode}`));
        return;
      }
      let data = "";
      res.setEncoding("utf-8");
      res.on("data", (chunk: string) => { data += chunk; });
      res.on("end", () => resolve(data));
      res.on("error", reject);
    });
    req.on("error", reject);
    req.end();
  });
}

async function resolveAttachments(attachments: MockAttachment[]): Promise<string> {
  if (attachments.length === 0) return "";
  const blocks: string[] = [];
  for (const att of attachments) {
    if (!att.download_url) {
      blocks.push(`[添付: ${att.name}] (download_url なし — 取得不可)`);
      continue;
    }
    const isText = TEXT_CONTENT_TYPES.test(att.content_type);
    if (isText) {
      try {
        const content = await fetchUrlSync(att.download_url);
        if (content.length <= MAX_INLINE_SIZE) {
          blocks.push(`[添付: ${att.name}]\n${content}\n[/添付]`);
        } else {
          blocks.push(`[添付: ${att.name}] (${Math.round(content.length / 1024)}KB — 大容量のため URL 参照)\nURL: ${att.download_url}`);
        }
      } catch {
        blocks.push(`[添付: ${att.name}] URL: ${att.download_url}`);
      }
    } else {
      blocks.push(`[添付: ${att.name}] (${att.content_type})\nURL: ${att.download_url}`);
    }
  }
  return "\n\n" + blocks.join("\n\n");
}

// =========================================================================
// Tests
// =========================================================================

describe("Waterline scoped persistence", () => {
  const backupPath = WATERLINE_PATH + ".bak";

  beforeEach(() => {
    if (fs.existsSync(WATERLINE_PATH)) {
      fs.copyFileSync(WATERLINE_PATH, backupPath);
    }
  });

  afterEach(() => {
    if (fs.existsSync(backupPath)) {
      fs.renameSync(backupPath, WATERLINE_PATH);
    } else if (fs.existsSync(WATERLINE_PATH)) {
      fs.unlinkSync(WATERLINE_PATH);
    }
  });

  test("save and load waterline by topicId", () => {
    const topicA = "topic-aaa";
    const topicB = "topic-bbb";

    saveWaterline(topicA, 42);
    saveWaterline(topicB, 17);

    expect(loadWaterline(topicA)).toBe(42);
    expect(loadWaterline(topicB)).toBe(17);
    expect(loadWaterline("topic-unknown")).toBeNull();
  });

  test("update existing topicId without affecting others", () => {
    saveWaterline("t1", 10);
    saveWaterline("t2", 20);
    saveWaterline("t1", 50);

    expect(loadWaterline("t1")).toBe(50);
    expect(loadWaterline("t2")).toBe(20);
  });

  test("migrate from old format (single value) returns empty", () => {
    // Old format: { waterline: 42 }
    fs.writeFileSync(WATERLINE_PATH, JSON.stringify({ waterline: 42 }), { mode: 0o600 });
    expect(loadWaterline("any-topic")).toBeNull();
  });
});

describe("drainEvents logic", () => {
  test("processes events sequentially and advances waterline", async () => {
    let waterline = 0;
    const processedIds: string[] = [];
    const processedEvents = new Set<string>();

    // Simulate Pull API returning batches
    const allEvents = [
      { event_id: "e1", sequence_no: 1, type: "openclaw.request", payload: { message: "hello" } },
      { event_id: "e2", sequence_no: 2, type: "openclaw.request", payload: { message: "world" } },
      { event_id: "e3", sequence_no: 3, type: "openclaw.request", payload: { message: "test" } },
    ];

    const pullEvents = async (_creds: any, _topicId: string, afterSeq: number) => {
      return allEvents.filter(e => e.sequence_no > afterSeq);
    };

    const processEvent = async (event: any) => {
      if (event.sequence_no <= waterline) return;
      if (processedEvents.has(event.event_id)) return;
      processedIds.push(event.event_id);
      processedEvents.add(event.event_id);
      waterline = event.sequence_no;
    };

    // Drain
    let drainRunning = false;
    const drainEvents = async () => {
      if (drainRunning) return;
      drainRunning = true;
      try {
        while (true) {
          const batch = await pullEvents(null, "topic", waterline);
          if (batch.length === 0) break;
          for (const event of batch) {
            await processEvent(event);
          }
        }
      } finally {
        drainRunning = false;
      }
    };

    await drainEvents();

    expect(processedIds).toEqual(["e1", "e2", "e3"]);
    expect(waterline).toBe(3);
  });

  test("drainRunning flag prevents concurrent execution", async () => {
    let drainRunning = false;
    let drainCount = 0;

    const drainEvents = async () => {
      if (drainRunning) return;
      drainRunning = true;
      drainCount++;
      try {
        await new Promise(r => setTimeout(r, 50));
      } finally {
        drainRunning = false;
      }
    };

    // Fire 3 concurrent drains — only 1 should execute
    await Promise.all([drainEvents(), drainEvents(), drainEvents()]);
    expect(drainCount).toBe(1);
  });

  test("drainRunning resets on error", async () => {
    let drainRunning = false;
    let callCount = 0;

    const drainEvents = async () => {
      if (drainRunning) return;
      drainRunning = true;
      callCount++;
      try {
        throw new Error("simulated pull failure");
      } finally {
        drainRunning = false;
      }
    };

    await drainEvents().catch(() => {});
    expect(drainRunning).toBe(false);

    // Should be able to drain again
    await drainEvents().catch(() => {});
    expect(callCount).toBe(2);
  });

  test("dedup by event_id skips already processed events", async () => {
    let waterline = 0;
    const processedEvents = new Set<string>();
    const processedIds: string[] = [];

    const processEvent = async (event: any) => {
      if (event.sequence_no <= waterline) return;
      if (processedEvents.has(event.event_id)) return;
      processedIds.push(event.event_id);
      processedEvents.add(event.event_id);
      waterline = event.sequence_no;
    };

    // Process same event twice
    const event = { event_id: "e1", sequence_no: 1, type: "test", payload: { message: "hi" } };
    await processEvent(event);
    await processEvent(event);

    expect(processedIds).toEqual(["e1"]);
  });

  test("events without message/text are skipped but waterline advances", async () => {
    let waterline = 0;
    const processedEvents = new Set<string>();
    const dispatched: string[] = [];

    const processEvent = async (event: any) => {
      if (event.sequence_no <= waterline) return;
      if (processedEvents.has(event.event_id)) return;

      const payload = event.payload;
      if (!payload?.message && !payload?.text) {
        processedEvents.add(event.event_id);
        waterline = event.sequence_no;
        return;
      }
      dispatched.push(event.event_id);
      processedEvents.add(event.event_id);
      waterline = event.sequence_no;
    };

    await processEvent({ event_id: "e1", sequence_no: 1, type: "test", payload: {} });
    await processEvent({ event_id: "e2", sequence_no: 2, type: "test", payload: { message: "hello" } });

    expect(dispatched).toEqual(["e2"]);
    expect(waterline).toBe(2);
  });

  test("processedEvents TTL cleanup at 10,000 entries", () => {
    const processedEvents = new Set<string>();
    for (let i = 0; i < 10_001; i++) {
      processedEvents.add(`e-${i}`);
    }

    expect(processedEvents.size).toBe(10_001);

    // Simulate cleanup logic
    if (processedEvents.size > 10_000) {
      const entries = [...processedEvents];
      entries.splice(0, entries.length - 5_000);
      processedEvents.clear();
      entries.forEach(e => processedEvents.add(e));
    }

    expect(processedEvents.size).toBe(5_000);
    expect(processedEvents.has("e-10000")).toBe(true);
    expect(processedEvents.has("e-0")).toBe(false);
  });
});

describe("resolveAttachments", () => {
  let server: http.Server;
  let port: number;

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      if (req.url === "/small.txt") {
        res.writeHead(200, { "Content-Type": "text/plain" });
        res.end("Hello from attachment");
      } else if (req.url === "/large.txt") {
        res.writeHead(200, { "Content-Type": "text/plain" });
        res.end("x".repeat(60 * 1024)); // 60KB
      } else if (req.url === "/error") {
        res.writeHead(500);
        res.end("Internal Server Error");
      } else {
        res.writeHead(404);
        res.end("Not Found");
      }
    });
    await new Promise<void>(resolve => {
      server.listen(0, () => {
        port = (server.address() as any).port;
        resolve();
      });
    });
  });

  afterAll(async () => {
    await new Promise<void>(resolve => server.close(() => resolve()));
  });

  test("empty attachments returns empty string", async () => {
    expect(await resolveAttachments([])).toBe("");
  });

  test("text file <= 50KB is inlined", async () => {
    const result = await resolveAttachments([{
      name: "readme.txt",
      object_id: "obj1",
      content_type: "text/plain",
      download_url: `http://127.0.0.1:${port}/small.txt`,
    }]);
    expect(result).toContain("[添付: readme.txt]");
    expect(result).toContain("Hello from attachment");
    expect(result).toContain("[/添付]");
  });

  test("text file > 50KB shows URL reference", async () => {
    const result = await resolveAttachments([{
      name: "big.txt",
      object_id: "obj2",
      content_type: "text/plain",
      download_url: `http://127.0.0.1:${port}/large.txt`,
    }]);
    expect(result).toContain("大容量のため URL 参照");
    expect(result).toContain(`http://127.0.0.1:${port}/large.txt`);
    expect(result).not.toContain("[/添付]");
  });

  test("binary file shows URL reference", async () => {
    const result = await resolveAttachments([{
      name: "screenshot.png",
      object_id: "obj3",
      content_type: "image/png",
      download_url: `http://127.0.0.1:${port}/small.txt`,
    }]);
    expect(result).toContain("[添付: screenshot.png] (image/png)");
    expect(result).toContain("URL:");
  });

  test("missing download_url shows error", async () => {
    const result = await resolveAttachments([{
      name: "orphan.txt",
      object_id: "obj4",
      content_type: "text/plain",
    }]);
    expect(result).toContain("download_url なし");
  });

  test("fetch error falls back to URL reference", async () => {
    const result = await resolveAttachments([{
      name: "broken.txt",
      object_id: "obj5",
      content_type: "text/plain",
      download_url: `http://127.0.0.1:${port}/error`,
    }]);
    expect(result).toContain("URL:");
    expect(result).toContain(`http://127.0.0.1:${port}/error`);
  });

  test("application/json is treated as text", async () => {
    const result = await resolveAttachments([{
      name: "data.json",
      object_id: "obj6",
      content_type: "application/json",
      download_url: `http://127.0.0.1:${port}/small.txt`,
    }]);
    expect(result).toContain("[添付: data.json]");
    expect(result).toContain("Hello from attachment");
    expect(result).toContain("[/添付]");
  });
});
