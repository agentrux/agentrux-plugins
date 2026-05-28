// Topology Request Flow v1 install — TypeScript SDK helper unit tests
//
// SSOT: docs/04_design/auth/topology_request_v1.md + src/topology-install.ts
// 4 軸 (a 正常 / b エラー / c 境界 / d 攻撃):

import {
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
  type TopologyDeclaration,
  buildAuthorizationDetails,
  installTopology,
  validateDeclaration,
} from "../topology-install";

function baseDecl(overrides: Partial<TopologyDeclaration> = {}): TopologyDeclaration {
  return {
    script_name: "weather-bot",
    description: "WeatherAPI を Composer に流す",
    topics: [
      { ref: "weather-data", name: "weather-data", retention_s: 86400, intent: "publish 1h" },
    ],
    grants: [
      { topic_ref: "weather-data", scope: "write", binding_name: "weather-out" },
    ],
    ...overrides,
  };
}

function tokenSuccessBody(): Record<string, unknown> {
  return {
    access_token: "aat_TEST",
    refresh_token: "art_TEST",
    token_type: "Bearer",
    expires_in: 600,
    scope: "topic.write topic:top_T:write",
    authorization_details: [
      {
        type: "agentrux.topology",
        version: 1,
        granted: {
          script_id: "scr_S",
          alias_id: "ali_A",
          topic_id_map: { "weather-data": "top_T" },
          grant_ids: {
            "topic:top_T:write": { grant_id: "grt_G", binding_name: "weather-out" },
          },
        },
      },
    ],
  };
}

/** Stub fetch that walks a series of expected (urlSuffix, status, body) tuples. */
function makeFetch(
  steps: Array<{ path: string; status: number; body: unknown }>,
): typeof fetch {
  const it = steps[Symbol.iterator]();
  return ((async (url: string | URL | Request, _init?: RequestInit) => {
    const u = typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
    const next = it.next();
    if (next.done) {
      return new Response(JSON.stringify({ error: "test exhausted steps" }), { status: 500 });
    }
    const step = next.value;
    if (!u.endsWith(step.path)) {
      return new Response(
        JSON.stringify({ error: `path mismatch want=${step.path} got=${u}` }),
        { status: 599 },
      );
    }
    const bodyStr = typeof step.body === "string" ? step.body : JSON.stringify(step.body);
    return new Response(bodyStr, {
      status: step.status,
      headers: { "content-type": "application/json" },
    });
  }) as unknown) as typeof fetch;
}

// ---------------------------------------------------------------------------
// a. validation (sync, no fetch)
// ---------------------------------------------------------------------------

describe("validateDeclaration", () => {
  test("a1: valid declaration passes", () => {
    expect(() => validateDeclaration(baseDecl())).not.toThrow();
  });

  test("a2: missing topics throws", () => {
    expect(() =>
      validateDeclaration(baseDecl({ topics: [] })),
    ).toThrow(InstallConfigError);
  });

  test("a3: missing grants throws", () => {
    expect(() =>
      validateDeclaration(baseDecl({ grants: [] })),
    ).toThrow(InstallConfigError);
  });

  test("a4: grant.topic_ref not in topics", () => {
    expect(() =>
      validateDeclaration(
        baseDecl({
          grants: [{ topic_ref: "missing", scope: "read" }],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("a5: duplicate (topic_ref, scope)", () => {
    expect(() =>
      validateDeclaration(
        baseDecl({
          grants: [
            { topic_ref: "weather-data", scope: "write", binding_name: "b1" },
            { topic_ref: "weather-data", scope: "write", binding_name: "b2" },
          ],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("a6: duplicate binding_name across topics", () => {
    expect(() =>
      validateDeclaration({
        script_name: "x",
        description: "x",
        topics: [
          { ref: "a", name: "a", retention_s: 3600 },
          { ref: "b", name: "b", retention_s: 3600 },
        ],
        grants: [
          { topic_ref: "a", scope: "read", binding_name: "shared" },
          { topic_ref: "b", scope: "read", binding_name: "shared" },
        ],
      }),
    ).toThrow(InstallConfigError);
  });

  test("a7: version != 1 rejected", () => {
    expect(() => validateDeclaration(baseDecl({ version: 2 }))).toThrow(InstallConfigError);
  });
});

describe("buildAuthorizationDetails", () => {
  test("a8: serialization shape", () => {
    const json = buildAuthorizationDetails(baseDecl());
    const parsed = JSON.parse(json);
    expect(parsed).toHaveLength(1);
    expect(parsed[0].type).toBe("agentrux.topology");
    expect(parsed[0].version).toBe(1);
    expect(parsed[0].script.name).toBe("weather-bot");
    expect(parsed[0].topics).toHaveLength(1);
    expect(parsed[0].topics[0].ref).toBe("weather-data");
    expect(parsed[0].grants[0].binding_name).toBe("weather-out");
    expect(parsed[0].policy_match_inputs).toBeNull();
  });

  test("a9: intent absent → null", () => {
    const d = baseDecl();
    delete (d.topics[0] as { intent?: unknown }).intent;
    const parsed = JSON.parse(buildAuthorizationDetails(d));
    expect(parsed[0].topics[0].intent).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// b. installTopology — happy path
// ---------------------------------------------------------------------------

describe("installTopology happy path", () => {
  test("b1: issue → 1 poll → success", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "ABCD-EFGH",
          verification_uri: "https://app.agentrux.com/topology/approve",
          verification_uri_complete: "https://app.agentrux.com/topology/approve?code=ABCD-EFGH",
          expires_in: 600,
          interval: 0, // 0 で即時 poll (test 高速化)
        },
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);

    const seen: string[] = [];
    const result = await installTopology({
      baseUrl: "https://api.example.com",
      clientId: "client-uuid",
      declaration: baseDecl(),
      onUserCode: (info) => {
        seen.push(info.userCode);
      },
      fetchImpl,
    });

    expect(result.accessToken).toBe("aat_TEST");
    expect(result.refreshToken).toBe("art_TEST");
    expect(result.scriptId).toBe("scr_S");
    expect(result.aliasId).toBe("ali_A");
    expect(result.topicIdMap["weather-data"]).toBe("top_T");
    expect(result.grants).toHaveLength(1);
    expect(result.grants[0].grantId).toBe("grt_G");
    expect(result.grants[0].bindingName).toBe("weather-out");
    expect(result.scope).toContain("topic.write");
    expect(seen).toEqual(["ABCD-EFGH"]);
  });

  test("b2: async onUserCode awaited", async () => {
    let asyncDone = false;
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    await installTopology({
      baseUrl: "https://api.example.com",
      clientId: "client-uuid",
      declaration: baseDecl(),
      onUserCode: async () => {
        await new Promise((r) => setTimeout(r, 1));
        asyncDone = true;
      },
      fetchImpl,
    });
    expect(asyncDone).toBe(true);
  });

  test("b3: authorization_pending → success (interval 0)", async () => {
    // slow_down (interval+5s) を含めると jest default timeout に引っかかるため、
    // authorization_pending → success の路だけ確認 (slow_down は Python 側でカバー)。
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      { path: "/oauth/token", status: 400, body: { error: "authorization_pending" } },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const r = await installTopology({
      baseUrl: "https://api.example.com",
      clientId: "client-uuid",
      declaration: baseDecl(),
      onUserCode: () => undefined,
      fetchImpl,
    });
    expect(r.accessToken).toBe("aat_TEST");
  }, 15_000);
});

// ---------------------------------------------------------------------------
// c. errors
// ---------------------------------------------------------------------------

describe("installTopology errors", () => {
  test("c1: access_denied → InstallDeniedError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      { path: "/oauth/token", status: 400, body: { error: "access_denied" } },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallDeniedError);
  });

  test("c2: expired_token → InstallTimeoutError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      { path: "/oauth/token", status: 400, body: { error: "expired_token" } },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallTimeoutError);
  });

  test("c3: invalid_client at issue → InstallAuthError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 400,
        body: { detail: { error: "invalid_client", error_description: "unknown" } },
      },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "bogus",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallAuthError);
  });

  test("c4: unsupported_authorization_details_version → InstallError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 400,
        body: {
          detail: {
            error: "unsupported_authorization_details_version",
            error_description: "v2",
          },
        },
      },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallError);
  });

  test("c5: 200 OK but no authorization_details → InstallError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      {
        path: "/oauth/token",
        status: 200,
        body: {
          access_token: "aat_T",
          refresh_token: "art_T",
          token_type: "Bearer",
          expires_in: 600,
          scope: "",
        },
      },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallError);
  });
});

// ---------------------------------------------------------------------------
// d. config / attack
// ---------------------------------------------------------------------------

describe("installTopology config", () => {
  test("d1: invalid base URL", async () => {
    await expect(
      installTopology({
        baseUrl: "ftp://bogus",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("d2: empty clientId", async () => {
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "",
        declaration: baseDecl(),
        onUserCode: () => undefined,
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("d3: AbortSignal cancels polling with InstallAbortedError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 60, // 60s interval — would block test without abort
        },
      },
    ]);
    const ctrl = new AbortController();
    const p = installTopology({
      baseUrl: "https://api.example.com",
      clientId: "c",
      declaration: baseDecl(),
      onUserCode: () => {
        // abort 即発火 → delay() が reject する
        ctrl.abort();
      },
      signal: ctrl.signal,
      fetchImpl,
    });
    // Codex MF-4: AbortSignal → InstallAbortedError (Python の InstallAbortedError と symmetric)
    await expect(p).rejects.toBeInstanceOf(InstallAbortedError);
  });
});

// ---------------------------------------------------------------------------
// Codex MF-1 補完: client-side validation
// ---------------------------------------------------------------------------

describe("validateDeclaration (Codex MF-1)", () => {
  test("v1: invalid scope rejected", () => {
    expect(() =>
      validateDeclaration(
        baseDecl({
          grants: [
            { topic_ref: "weather-data", scope: "admin" as unknown as "read" },
          ],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("v2: binding_name too long rejected", () => {
    expect(() =>
      validateDeclaration(
        baseDecl({
          grants: [
            { topic_ref: "weather-data", scope: "read", binding_name: "x".repeat(65) },
          ],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("v3: binding_name leading space rejected", () => {
    expect(() =>
      validateDeclaration(
        baseDecl({
          grants: [
            { topic_ref: "weather-data", scope: "read", binding_name: " foo" },
          ],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("v4: description too long rejected", () => {
    expect(() =>
      validateDeclaration(baseDecl({ description: "d".repeat(257) })),
    ).toThrow(InstallConfigError);
  });

  test("v5: topics count over limit rejected", () => {
    const topics = Array.from({ length: 21 }, (_, i) => ({
      ref: `t${i}`,
      name: `t${i}`,
      retention_s: 3600,
    }));
    expect(() =>
      validateDeclaration(
        baseDecl({
          topics,
          grants: [{ topic_ref: "t0", scope: "read" }],
        }),
      ),
    ).toThrow(InstallConfigError);
  });

  test("v6: control char in description rejected", () => {
    expect(() =>
      validateDeclaration(baseDecl({ description: "hello\x00world" })),
    ).toThrow(InstallConfigError);
  });
});

// ---------------------------------------------------------------------------
// Codex MF-2: token response shape strict validation
// ---------------------------------------------------------------------------

describe("parseTokenResponse strict shape (Codex MF-2)", () => {
  test("v7: missing script_id rejected", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      {
        path: "/oauth/token",
        status: 200,
        body: {
          access_token: "aat_T",
          refresh_token: "art_T",
          token_type: "Bearer",
          expires_in: 600,
          scope: "topic.write",
          authorization_details: [
            {
              type: "agentrux.topology",
              version: 1,
              granted: {
                // script_id 欠落
                alias_id: "ali_A",
                topic_id_map: {},
                grant_ids: {},
              },
            },
          ],
        },
      },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallError);
  });

  test("v8: malformed grant_ids entry rejected", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/topology-request",
        status: 200,
        body: {
          device_code: "dc_X",
          user_code: "X-Y",
          verification_uri: "u",
          verification_uri_complete: "u",
          expires_in: 600,
          interval: 0,
        },
      },
      {
        path: "/oauth/token",
        status: 200,
        body: {
          access_token: "aat_T",
          refresh_token: "art_T",
          token_type: "Bearer",
          expires_in: 600,
          scope: "topic.write",
          authorization_details: [
            {
              type: "agentrux.topology",
              version: 1,
              granted: {
                script_id: "scr_S",
                alias_id: "ali_A",
                topic_id_map: { "weather-data": "top_T" },
                grant_ids: {
                  "topic:top_T:write": { grant_id: 12345 }, // int (not string)
                },
              },
            },
          ],
        },
      },
    ]);
    await expect(
      installTopology({
        baseUrl: "https://api.example.com",
        clientId: "c",
        declaration: baseDecl(),
        onUserCode: () => undefined,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallError);
  });
});
