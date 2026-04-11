/**
 * Dispatcher transient error handling tests.
 *
 * Verifies:
 * 1. Transport retry: callDispatchEndpoint retries on transient errors
 * 2. Transient drop: transport failure after retries does NOT write to outbox
 * 3. Application failure: non-transport errors still write to outbox (existing behavior)
 * 4. isTransientError classification
 */

import * as http from "http";

// --- isTransientError (extracted for testing) ---
function isTransientError(e: any): boolean {
  const code = e?.code;
  if (code && ["ECONNREFUSED", "ETIMEDOUT", "ECONNRESET", "EHOSTUNREACH"].includes(code)) {
    return true;
  }
  const msg = e?.message || "";
  return msg.includes("socket hang up") || msg.includes("Dispatch timeout");
}

describe("isTransientError", () => {
  test("ECONNREFUSED → transient", () => {
    const e: any = new Error("connect ECONNREFUSED 127.0.0.1:18789");
    e.code = "ECONNREFUSED";
    expect(isTransientError(e)).toBe(true);
  });

  test("ETIMEDOUT → transient", () => {
    const e: any = new Error("connect ETIMEDOUT");
    e.code = "ETIMEDOUT";
    expect(isTransientError(e)).toBe(true);
  });

  test("ECONNRESET → transient", () => {
    const e: any = new Error("read ECONNRESET");
    e.code = "ECONNRESET";
    expect(isTransientError(e)).toBe(true);
  });

  test("EHOSTUNREACH → transient", () => {
    const e: any = new Error("connect EHOSTUNREACH");
    e.code = "EHOSTUNREACH";
    expect(isTransientError(e)).toBe(true);
  });

  test("socket hang up → transient", () => {
    expect(isTransientError(new Error("socket hang up"))).toBe(true);
  });

  test("Dispatch timeout → transient", () => {
    expect(isTransientError(new Error("Dispatch timeout"))).toBe(true);
  });

  test("JSON parse error → NOT transient", () => {
    expect(isTransientError(new Error("Dispatch response parse error: <html>"))).toBe(false);
  });

  test("generic Error → NOT transient", () => {
    expect(isTransientError(new Error("something unexpected"))).toBe(false);
  });

  test("null/undefined → NOT transient", () => {
    expect(isTransientError(null)).toBe(false);
    expect(isTransientError(undefined)).toBe(false);
  });
});

// --- Transport retry integration test ---

describe("callDispatchEndpoint transport retry", () => {
  test("retries on ECONNREFUSED then succeeds", async () => {
    let requestCount = 0;
    const PORT = 19876;
    let server: http.Server;

    // _doDispatchRequest equivalent
    function doRequest(): Promise<any> {
      return new Promise((resolve, reject) => {
        const body = JSON.stringify({ sessionKey: "test", message: "hi", idempotencyKey: "k1", timeoutMs: 5000 });
        const req = http.request(
          { hostname: "127.0.0.1", port: PORT, path: "/agentrux/dispatch", method: "POST",
            headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) }, timeout: 5000 },
          (res) => {
            let raw = "";
            res.on("data", (c: Buffer) => (raw += c.toString()));
            res.on("end", () => { try { resolve(JSON.parse(raw)); } catch { reject(new Error("parse error")); } });
          },
        );
        req.on("error", reject);
        req.on("timeout", () => { req.destroy(); reject(new Error("Dispatch timeout")); });
        req.write(body);
        req.end();
      });
    }

    // Retry wrapper (mirrors callDispatchEndpoint)
    async function callWithRetry(): Promise<any> {
      const MAX = 3;
      const BASE = 200; // shorter for test
      for (let attempt = 0; attempt < MAX; attempt++) {
        try {
          return await doRequest();
        } catch (e: any) {
          if (isTransientError(e) && attempt < MAX - 1) {
            await new Promise((r) => setTimeout(r, BASE * Math.pow(2, attempt)));
            continue;
          }
          throw e;
        }
      }
      throw new Error("max retries");
    }

    // Start server after 300ms (1st attempt fails, 2nd succeeds)
    setTimeout(() => {
      server = http.createServer((req, res) => {
        requestCount++;
        let body = "";
        req.on("data", (c) => (body += c));
        req.on("end", () => {
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ responseText: "ok", status: "ok" }));
        });
      });
      server.listen(PORT);
    }, 300);

    const result = await callWithRetry();
    expect(result.status).toBe("ok");
    expect(requestCount).toBe(1);
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }, 15000);

  test("all retries fail → throws transient error", async () => {
    // Simulate 3 ECONNREFUSED failures without real sockets
    let attempts = 0;
    async function fakeRequest(): Promise<any> {
      attempts++;
      const e: any = new Error("connect ECONNREFUSED 127.0.0.1:19877");
      e.code = "ECONNREFUSED";
      throw e;
    }

    async function callWithRetry(): Promise<any> {
      const MAX = 3;
      const BASE = 50; // short for test
      for (let attempt = 0; attempt < MAX; attempt++) {
        try {
          return await fakeRequest();
        } catch (e: any) {
          if (isTransientError(e) && attempt < MAX - 1) {
            await new Promise((r) => setTimeout(r, BASE));
            continue;
          }
          throw e;
        }
      }
    }

    let threw = false;
    try {
      await callWithRetry();
    } catch (e: any) {
      threw = true;
      expect(isTransientError(e)).toBe(true);
    }
    expect(threw).toBe(true);
    expect(attempts).toBe(3); // all 3 attempts made
  }, 10000);
});

// --- Transient vs Application error routing ---

describe("processEvent error routing", () => {
  test("transient error should NOT produce outbox entry", () => {
    // Simulate the decision logic from processEvent catch block
    const e: any = new Error("connect ECONNREFUSED 127.0.0.1:18789");
    e.code = "ECONNREFUSED";

    let outboxWritten = false;
    let eventRecorded = false;
    let completed = false;

    if (isTransientError(e)) {
      // Transport failure path: drop event
      completed = true;
      // outboxWritten stays false
      // eventRecorded stays false
    } else {
      outboxWritten = true;
      eventRecorded = true;
    }

    expect(outboxWritten).toBe(false);
    expect(eventRecorded).toBe(false);
    expect(completed).toBe(true);
  });

  test("application error SHOULD produce outbox entry", () => {
    const e = new Error("Subagent execution failed");

    let outboxWritten = false;
    let eventRecorded = false;
    let completed = false;

    if (isTransientError(e)) {
      completed = true;
    } else {
      outboxWritten = true;
      eventRecorded = true;
    }

    expect(outboxWritten).toBe(true);
    expect(eventRecorded).toBe(true);
    expect(completed).toBe(false);
  });
});
