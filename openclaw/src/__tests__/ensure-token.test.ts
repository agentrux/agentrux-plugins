/**
 * ensureToken tests (OAuth 2.1 client_credentials, Phase 1.9+).
 *
 * The bugs that motivated the original test file were specific to the old
 * refresh_token flow (race on a single-use refresh_token; dead-refresh stuck
 * in cache). client_credentials has no refresh leg — on expiry / 401 we
 * just re-issue against /oauth/token — so those tests no longer apply.
 *
 * What still matters and what these tests pin:
 *
 *   - Single-flight: concurrent callers must coalesce on one /oauth/token
 *     issue, not race on the rate-limited token endpoint.
 *   - Cache reuse: a valid cached token must not trigger a redundant HTTP
 *     round-trip (60s safety margin).
 *   - invalidateIfStillCurrent: a stale 401 (from a request that went out
 *     before a sibling refreshed) must NOT clobber the fresh cached token.
 *   - invalidateToken: explicit invalidation forces a re-issue on the next
 *     call.
 *
 * Implementation detail: ensureToken calls Node's `https.request` /
 * `http.request` via the httpForm helper. We mock those modules so the
 * tests exercise the real ensureToken control flow.
 */

import { EventEmitter } from "events";

const requestMock = jest.fn();
jest.mock("https", () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));
jest.mock("http", () => ({
  request: (...args: unknown[]) => requestMock(...args),
}));

type Reply = { status: number; body: Record<string, unknown> };

function fakeRequest(replies: Reply[]) {
  let i = 0;
  return (_opts: unknown, cb: (res: EventEmitter & { statusCode: number }) => void) => {
    const reply = replies[i++];
    if (!reply) {
      throw new Error(`fakeRequest: no more replies queued (call #${i})`);
    }
    const res: EventEmitter & { statusCode: number } = Object.assign(
      new EventEmitter(),
      { statusCode: reply.status },
    );
    setImmediate(() => {
      cb(res);
      res.emit("data", Buffer.from(JSON.stringify(reply.body)));
      res.emit("end");
    });
    const req: EventEmitter & { write: jest.Mock; end: jest.Mock } = Object.assign(
      new EventEmitter(),
      { write: jest.fn(), end: jest.fn() },
    );
    return req;
  };
}

const creds = {
  base_url: "https://api.example.test",
  client_id: "crd_test",
  client_secret: "aks_test_secret",
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
          body: { access_token: "AT-1", expires_in: 3600 },
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
});

describe("ensureToken — cache reuse + re-issue on expiry", () => {
  test("a valid cached token is reused without an HTTP call", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        { status: 200, body: { access_token: "AT-cached", expires_in: 3600 } },
      ]),
    );

    const { ensureToken } = loadHttpClient();
    const a = await ensureToken(creds);
    const b = await ensureToken(creds);

    expect(a).toBe("AT-cached");
    expect(b).toBe("AT-cached");
    expect(requestMock).toHaveBeenCalledTimes(1);
  });

  test("a cached token inside the 60s safety margin triggers a re-issue", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        // expires_in: 30 — inside the 60s safety margin, so the next call
        // must re-issue.
        { status: 200, body: { access_token: "AT-stale", expires_in: 30 } },
        { status: 200, body: { access_token: "AT-fresh", expires_in: 3600 } },
      ]),
    );

    const { ensureToken } = loadHttpClient();
    expect(await ensureToken(creds)).toBe("AT-stale");
    expect(await ensureToken(creds)).toBe("AT-fresh");
    expect(requestMock).toHaveBeenCalledTimes(2);
  });
});

describe("invalidateIfStillCurrent — stale-401 guard", () => {
  test("clears the cache only when the supplied token is still the cached one", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        { status: 200, body: { access_token: "AT-current", expires_in: 3600 } },
      ]),
    );
    const { ensureToken, invalidateIfStillCurrent } = loadHttpClient();
    await ensureToken(creds);

    // Stale 401 referencing a token we no longer have — must not wipe the live cache.
    expect(invalidateIfStillCurrent("AT-stale")).toBe(false);

    const reused = await ensureToken(creds);
    expect(reused).toBe("AT-current");
    expect(requestMock).toHaveBeenCalledTimes(1);

    // 401 referencing the live token — clear is allowed.
    expect(invalidateIfStillCurrent("AT-current")).toBe(true);
  });
});

describe("ensureToken — invalidateToken", () => {
  test("invalidateToken clears state so the next call re-auths", async () => {
    requestMock.mockImplementation(
      fakeRequest([
        { status: 200, body: { access_token: "AT-1", expires_in: 3600 } },
        { status: 200, body: { access_token: "AT-2", expires_in: 3600 } },
      ]),
    );
    const { ensureToken, invalidateToken } = loadHttpClient();
    expect(await ensureToken(creds)).toBe("AT-1");
    invalidateToken();
    expect(await ensureToken(creds)).toBe("AT-2");
    expect(requestMock).toHaveBeenCalledTimes(2);
  });
});
