/**
 * ensureToken tests — focused on the bugs that the previous implementation had:
 *
 *   Bug A: concurrent callers each issued their own /auth/refresh, racing
 *          on the same single-use refresh_token. Two of three calls would
 *          burn the rate limit and fall through to client_secret re-auth.
 *
 *   Bug B: when /auth/refresh failed, the dead refresh_token remained in
 *          tokenState, so the next caller would try it again and get the
 *          same failure forever.
 *
 * The fix is single-flight + clear-on-failure. These tests pin both.
 *
 * We test ensureToken without HTTP by stubbing the underlying http module
 * via jest.mock at the http-client level — but ensureToken itself lives in
 * http-client, so we instead mock the actual `https`/`http` request layer.
 * That is too invasive; the simpler approach is to import http-client and
 * stub `httpJson` on the module exports. Jest's module-level mocking is the
 * cleanest way: we replace http-client's own httpJson at the module export,
 * which forces ensureToken (which calls httpJson by name in the same module)
 * to go through our mock.
 *
 * Since ensureToken calls httpJson directly (not via `this.httpJson` or an
 * imported alias from a different module), we can't intercept it from
 * outside the file. So we test the behavior end-to-end by mocking Node's
 * `https.request` instead. That's verbose but it exercises the real
 * ensureToken control flow.
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

    // Build a fake response stream.
    const res: any = new EventEmitter();
    res.statusCode = reply.status;
    setImmediate(() => {
      cb(res);
      res.emit("data", Buffer.from(JSON.stringify(reply.body)));
      res.emit("end");
    });

    // Return a fake request object with write/end/on.
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

describe("ensureToken — single-flight (Bug A)", () => {
  test("three concurrent callers issue ONE /auth/token, not three", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: {
            access_token: "AT-1",
            refresh_token: "RT-1",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();

    // Three concurrent callers — they should all coalesce on the same
    // in-flight promise and get the same token without issuing extra
    // /auth/token calls.
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

  test("after a refresh, a fresh concurrent burst still issues ONE /auth/refresh", async () => {
    // Step 1: cold start → /auth/token returns an immediately-stale token
    //         (expires in the past after the 60s safety margin).
    // Step 2: three concurrent callers → must issue exactly one /auth/refresh.
    requestMock.mockImplementation(
      fakeRequest([
        // Initial /auth/token: short-lived (expires in 30s, which is inside
        // ensureToken's 60s safety margin, so it will be considered stale on
        // the very next call).
        {
          status: 200,
          body: {
            access_token: "AT-old",
            refresh_token: "RT-old",
            expires_at: new Date(Date.now() + 30_000).toISOString(),
          },
        },
        // Single /auth/refresh response shared by the three concurrent callers.
        {
          status: 200,
          body: {
            access_token: "AT-new",
            refresh_token: "RT-new",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();

    // Step 1.
    const t0 = await ensureToken(creds);
    expect(t0).toBe("AT-old");
    expect(requestMock).toHaveBeenCalledTimes(1);

    // Step 2: three concurrent refreshers.
    const [a, b, c] = await Promise.all([
      ensureToken(creds),
      ensureToken(creds),
      ensureToken(creds),
    ]);
    expect(a).toBe("AT-new");
    expect(b).toBe("AT-new");
    expect(c).toBe("AT-new");
    // Total: 1 (token) + 1 (refresh) = 2.
    expect(requestMock).toHaveBeenCalledTimes(2);
  });
});

describe("ensureToken — dead refresh_token cleanup (Bug B)", () => {
  test("a failed refresh clears tokenState and falls through to client_secret re-auth", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        // Initial /auth/token: stale-on-arrival.
        {
          status: 200,
          body: {
            access_token: "AT-old",
            refresh_token: "RT-old",
            expires_at: new Date(Date.now() + 30_000).toISOString(),
          },
        },
        // /auth/refresh fails (revoked).
        {
          status: 401,
          body: { error: { code: "UNAUTHORIZED", message: "revoked" } },
        },
        // /auth/token re-auth succeeds.
        {
          status: 200,
          body: {
            access_token: "AT-fresh",
            refresh_token: "RT-fresh",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();
    const first = await ensureToken(creds);
    expect(first).toBe("AT-old");

    const second = await ensureToken(creds);
    // After the dead refresh, we re-authed and got a fresh token.
    expect(second).toBe("AT-fresh");
    expect(requestMock).toHaveBeenCalledTimes(3);
  });

  test("the dead refresh_token is NOT retried on a subsequent call", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        // Initial token (stale).
        {
          status: 200,
          body: {
            access_token: "AT-1",
            refresh_token: "RT-1",
            expires_at: new Date(Date.now() + 30_000).toISOString(),
          },
        },
        // First refresh fails.
        {
          status: 401,
          body: { error: { code: "UNAUTHORIZED", message: "dead" } },
        },
        // Re-auth succeeds with a fresh token (also stale-on-arrival to force
        // another refresh attempt on the next call).
        {
          status: 200,
          body: {
            access_token: "AT-2",
            refresh_token: "RT-2",
            expires_at: new Date(Date.now() + 30_000).toISOString(),
          },
        },
        // Second refresh succeeds with the NEW refresh_token, not the dead one.
        {
          status: 200,
          body: {
            access_token: "AT-3",
            refresh_token: "RT-3",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
      ]),
    );

    const { ensureToken } = loadHttpClient();
    await ensureToken(creds); // AT-1
    await ensureToken(creds); // refresh RT-1 → 401, re-auth → AT-2
    await ensureToken(creds); // refresh RT-2 → AT-3

    // Total: 1 token + 1 (failed) refresh + 1 token + 1 (good) refresh = 4.
    expect(requestMock).toHaveBeenCalledTimes(4);
  });
});

describe("invalidateIfStillCurrent — stale-401 guard (Codex NICE-TO-HAVE)", () => {
  test("clears the cache only when the supplied token is still the cached one", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        {
          status: 200,
          body: {
            access_token: "AT-current",
            refresh_token: "RT-current",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
      ]),
    );
    const { ensureToken, invalidateIfStillCurrent } = loadHttpClient();
    await ensureToken(creds); // populate tokenState

    // Stale 401 from a request that used a token we no longer have:
    // we MUST NOT wipe the live cache.
    expect(invalidateIfStillCurrent("AT-stale")).toBe(false);

    // Calling ensureToken again should reuse the cached AT-current with no
    // new HTTP traffic.
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
          body: {
            access_token: "AT-1",
            refresh_token: "RT-1",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
        },
        {
          status: 200,
          body: {
            access_token: "AT-2",
            refresh_token: "RT-2",
            expires_at: new Date(Date.now() + 3600_000).toISOString(),
          },
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
