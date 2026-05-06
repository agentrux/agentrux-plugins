/**
 * ensureToken tests — focused on the surviving auth contract:
 *
 *   - single-flight: concurrent callers must coalesce on one /oauth/token,
 *     not race each other. Burning the rate limit was Bug A in the
 *     pre-OAuth implementation.
 *   - cache hits don't re-issue.
 *   - invalidateToken / invalidateIfStillCurrent flush correctly so the
 *     next caller re-auths but a stale 401 from an already-refreshed
 *     token does NOT wipe the cache.
 *
 * The old refresh_token branch is gone: `client_credentials` does not
 * issue refresh tokens (RFC 6749 §4.4), so there is exactly one auth
 * path here — POST /oauth/token form-encoded with the script credential
 * — and tests pin that.
 */

import { EventEmitter } from "events";

// Mock node:https before requiring http-client.
const requestMock = jest.fn();
jest.mock("https", () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));
jest.mock("http", () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));

type Reply = { status: number; body: any };

function fakeRequest(replies: Reply[]) {
  let i = 0;
  return (_opts: any, cb: (res: any) => void) => {
    const reply = replies[i++];
    if (!reply) {
      throw new Error(`fakeRequest: no more replies queued (call #${i})`);
    }
    const res: any = new EventEmitter();
    res.statusCode = reply.status;
    setImmediate(() => {
      cb(res);
      res.emit("data", Buffer.from(JSON.stringify(reply.body)));
      res.emit("end");
    });
    const req: any = new EventEmitter();
    req.write = jest.fn();
    req.end = jest.fn();
    return req;
  };
}

const creds = {
  base_url: "https://api.example.test",
  script_id: "scr_test",
  clientSecret: "secret_xyz",
};

beforeEach(() => {
  jest.resetModules();
  requestMock.mockReset();
});

function loadHttpClient() {
  return require("../http-client") as typeof import("../http-client");
}

describe("ensureToken — single-flight", () => {
  test("three concurrent callers issue ONE /oauth/token, not three", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: { access_token: "AT-1", token_type: "Bearer", expires_in: 3600 },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();

    const [a, b, c] = await Promise.all([
      ensureToken(creds),
      ensureToken(creds),
      ensureToken(creds),
    ]);

    expect(a).toBe("AT-1");
    expect(b).toBe("AT-1");
    expect(c).toBe("AT-1");
    expect(requestMock).toHaveBeenCalledTimes(1);
  });

  test("a fresh-after-expiry burst still issues ONE /oauth/token", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        // Initial token: short-lived enough that the 60s safety margin
        // marks it stale on the very next call.
        {
          status: 200,
          body: { access_token: "AT-old", token_type: "Bearer", expires_in: 30 },
        },
        // Re-issue.
        {
          status: 200,
          body: { access_token: "AT-new", token_type: "Bearer", expires_in: 3600 },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();

    expect(await ensureToken(creds)).toBe("AT-old");
    expect(requestMock).toHaveBeenCalledTimes(1);

    const [a, b, c] = await Promise.all([
      ensureToken(creds),
      ensureToken(creds),
      ensureToken(creds),
    ]);
    expect(a).toBe("AT-new");
    expect(b).toBe("AT-new");
    expect(c).toBe("AT-new");
    expect(requestMock).toHaveBeenCalledTimes(2);
  });
});

describe("ensureToken — cache hits", () => {
  test("a fresh token is reused without a network call", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: { access_token: "AT-cached", token_type: "Bearer", expires_in: 3600 },
        },
      ]),
    );
    const { ensureToken } = loadHttpClient();
    expect(await ensureToken(creds)).toBe("AT-cached");
    expect(await ensureToken(creds)).toBe("AT-cached");
    expect(await ensureToken(creds)).toBe("AT-cached");
    expect(requestMock).toHaveBeenCalledTimes(1);
  });
});

describe("invalidateIfStillCurrent — stale-401 guard", () => {
  test("clears the cache only when the supplied token is still the cached one", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: { access_token: "AT-current", token_type: "Bearer", expires_in: 3600 },
        },
      ]),
    );
    const { ensureToken, invalidateIfStillCurrent } = loadHttpClient();
    await ensureToken(creds);

    // Stale 401 with a token we no longer have: must NOT wipe the live cache.
    expect(invalidateIfStillCurrent("AT-stale")).toBe(false);

    const reused = await ensureToken(creds);
    expect(reused).toBe("AT-current");
    expect(requestMock).toHaveBeenCalledTimes(1);

    // 401 from the live token: clear is allowed.
    expect(invalidateIfStillCurrent("AT-current")).toBe(true);
  });
});

describe("ensureToken — invalidateToken", () => {
  test("invalidateToken clears state so the next call re-auths", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: { access_token: "AT-1", token_type: "Bearer", expires_in: 3600 },
        },
        {
          status: 200,
          body: { access_token: "AT-2", token_type: "Bearer", expires_in: 3600 },
        },
      ]),
    );
    const { ensureToken, invalidateToken } = loadHttpClient();
    expect(await ensureToken(creds)).toBe("AT-1");
    invalidateToken();
    expect(await ensureToken(creds)).toBe("AT-2");
    expect(requestMock).toHaveBeenCalledTimes(2);
  });
});

describe("ensureToken — request shape", () => {
  test("posts form-encoded grant_type=client_credentials with script_<id> client_id", async () => {
    let captured: { headers?: Record<string, string>; body?: string } = {};
    requestMock.mockImplementation((opts: any, cb: (res: any) => void) => {
      captured.headers = opts.headers;
      const res: any = new EventEmitter();
      res.statusCode = 200;
      setImmediate(() => {
        cb(res);
        res.emit("data", Buffer.from(JSON.stringify({
          access_token: "AT-x", token_type: "Bearer", expires_in: 3600,
        })));
        res.emit("end");
      });
      const req: any = new EventEmitter();
      req.write = jest.fn((chunk: any) => {
        captured.body = typeof chunk === "string" ? chunk : chunk.toString();
      });
      req.end = jest.fn();
      return req;
    });
    const { ensureToken } = loadHttpClient();
    await ensureToken(creds);
    expect(captured.headers?.["Content-Type"]).toBe("application/x-www-form-urlencoded");
    expect(captured.body).toContain("grant_type=client_credentials");
    expect(captured.body).toContain("client_id=script_scr_test");
    expect(captured.body).toContain("client_secret=secret_xyz");
  });
});
