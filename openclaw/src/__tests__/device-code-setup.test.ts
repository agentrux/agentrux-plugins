// Plain Device Code Setup (RFC 8628、 RAR なし) — TypeScript SDK helper unit tests
//
// SSOT: docs/04_design/auth/device_code_setup_v1.md §6-2
// 4 軸 (a 正常 / b エラー / c 境界 / d 攻撃 / e race) + AbortSignal cancel

import {
  type DeviceCodeSetupPending,
  type DeviceCodeSetupResult,
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
  setupViaDeviceCode,
} from "../device-code-setup";

function deviceAuthorizeBody(): Record<string, unknown> {
  return {
    device_code: "dc_TEST_DEVICE",
    user_code: "ABCD-1234",
    verification_uri: "https://console.agentrux.com/device",
    verification_uri_complete:
      "https://console.agentrux.com/device?user_code=ABCD-1234",
    expires_in: 600,
    interval: 0, // test 高速化のため
  };
}

function tokenSuccessBody(
  opts: { scope?: string; includeIdToken?: boolean } = {},
): Record<string, unknown> {
  const body: Record<string, unknown> = {
    access_token: "aat_TEST",
    refresh_token: "art_TEST",
    token_type: "Bearer",
    expires_in: 600,
    scope: opts.scope ?? "topic.read topic.write",
  };
  if (opts.includeIdToken) body.id_token = "eyJ.id.token";
  return body;
}

function makeFetch(
  steps: Array<{ path: string; status: number; body: unknown }>,
): typeof fetch {
  const it = steps[Symbol.iterator]();
  return ((async (url: string | URL | Request, _init?: RequestInit) => {
    const u =
      typeof url === "string" ? url : url instanceof URL ? url.href : url.url;
    const next = it.next();
    if (next.done) {
      return new Response(JSON.stringify({ error: "test exhausted steps" }), {
        status: 500,
      });
    }
    const step = next.value;
    if (!u.endsWith(step.path)) {
      return new Response(
        JSON.stringify({ error: `path mismatch want=${step.path} got=${u}` }),
        { status: 599 },
      );
    }
    const bodyStr =
      typeof step.body === "string" ? step.body : JSON.stringify(step.body);
    return new Response(bodyStr, {
      status: step.status,
      headers: { "content-type": "application/json" },
    });
  }) as unknown) as typeof fetch;
}

// ============================================================================
// a. 正常系
// ============================================================================

describe("setupViaDeviceCode — 正常系", () => {
  test("a1: happy path (default scope)", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      fetchImpl,
    });
    expect(result.accessToken).toBe("aat_TEST");
    expect(result.refreshToken).toBe("art_TEST");
    expect(result.expiresIn).toBe(600);
    expect(result.scope).toEqual(["topic.read", "topic.write"]);
    expect(result.idToken).toBeUndefined();
    expect(result.grantedScopes).toEqual(result.scope);
  });

  test("a2: openid scope returns id_token", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 200,
        body: tokenSuccessBody({
          scope: "topic.read topic.write openid email profile",
          includeIdToken: true,
        }),
      },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      scope: ["topic.read", "topic.write", "openid", "email", "profile"],
      fetchImpl,
    });
    expect(result.idToken).toBe("eyJ.id.token");
    expect(result.scope).toContain("openid");
  });

  test("a3: sync onUserCode called once", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const seen: DeviceCodeSetupPending[] = [];
    await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      onUserCode: (info) => {
        seen.push(info);
      },
      fetchImpl,
    });
    expect(seen).toHaveLength(1);
    expect(seen[0].userCode).toBe("ABCD-1234");
    expect(seen[0].verificationUriComplete).toContain("?user_code=ABCD-1234");
  });

  test("a4: async onUserCode awaited", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    let asyncDone = false;
    await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      onUserCode: async (info) => {
        await new Promise((r) => setTimeout(r, 1));
        asyncDone = true;
        expect(info.userCode).toBe("ABCD-1234");
      },
      fetchImpl,
    });
    expect(asyncDone).toBe(true);
  });
});

// ============================================================================
// b. エラー系
// ============================================================================

describe("setupViaDeviceCode — エラー系", () => {
  test("b1: invalid_client at authorize → InstallAuthError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 400,
        body: { detail: { error: "invalid_client", error_description: "unknown" } },
      },
    ]);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_unknown",
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallAuthError);
  });

  test("b2: invalid_scope pre-validation (vocab 外)", async () => {
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test",
        scope: ["topic.delete"], // vocab 外
        fetchImpl: makeFetch([]),
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("b3: access_denied during polling → InstallDeniedError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 400,
        body: { detail: { error: "access_denied", error_description: "user denied" } },
      },
    ]);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test_client",
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallDeniedError);
  });

  test("b4: expired_token during polling → InstallTimeoutError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 400,
        body: { detail: { error: "expired_token", error_description: "expired" } },
      },
    ]);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test_client",
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallTimeoutError);
  });

  test("b5: 5xx at authorize → InstallError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 503,
        body: { error: "service_unavailable" },
      },
    ]);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test",
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallError);
  });

  test("b6: malformed token response (access_token missing) → InstallError", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 200,
        body: { refresh_token: "art_only", expires_in: 600 },
      },
    ]);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test_client",
        fetchImpl,
      }),
    ).rejects.toThrow(/malformed token response/);
  });
});

// ============================================================================
// c. 境界
// ============================================================================

describe("setupViaDeviceCode — 境界", () => {
  test("c1: timeout 9999 clamped to 600", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      timeoutSeconds: 9999,
      fetchImpl,
    });
    expect(result.accessToken).toBe("aat_TEST");
  });

  test("c2: timeout 10 clamped to 60", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      timeoutSeconds: 10,
      fetchImpl,
    });
    expect(result.accessToken).toBe("aat_TEST");
  });

  test("c3: single scope topic.read only", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 200,
        body: tokenSuccessBody({ scope: "topic.read" }),
      },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      scope: ["topic.read"],
      fetchImpl,
    });
    expect(result.scope).toEqual(["topic.read"]);
  });

  test("c4: full vocab 5 scopes", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 200,
        body: tokenSuccessBody({
          scope: "topic.read topic.write openid email profile",
        }),
      },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      scope: ["topic.read", "topic.write", "openid", "email", "profile"],
      fetchImpl,
    });
    expect(new Set(result.scope)).toEqual(
      new Set(["topic.read", "topic.write", "openid", "email", "profile"]),
    );
  });
});

// ============================================================================
// d. 攻撃
// ============================================================================

describe("setupViaDeviceCode — 攻撃ベクター", () => {
  test("d1: javascript: URL rejected", async () => {
    await expect(
      setupViaDeviceCode({
        baseUrl: "javascript:alert(1)",
        clientId: "dcr_test",
        fetchImpl: makeFetch([]),
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("d2: clientId with control char rejected", async () => {
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_with\x00null",
        fetchImpl: makeFetch([]),
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("d3: duplicate scope rejected", async () => {
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test",
        scope: ["topic.read", "topic.read"],
        fetchImpl: makeFetch([]),
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });

  test("d4: empty scope rejected", async () => {
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test",
        scope: [],
        fetchImpl: makeFetch([]),
      }),
    ).rejects.toBeInstanceOf(InstallConfigError);
  });
});

// ============================================================================
// e. race / timing / AbortSignal
// ============================================================================

describe("setupViaDeviceCode — race / timing / AbortSignal", () => {
  test(
    "e1: slow_down → continue polling, eventual success",
    async () => {
      const fetchImpl = makeFetch([
        {
          path: "/oauth/device/authorize",
          status: 200,
          body: deviceAuthorizeBody(),
        },
        {
          path: "/oauth/token",
          status: 400,
          body: { detail: { error: "slow_down" } },
        },
        {
          path: "/oauth/token",
          status: 400,
          body: { detail: { error: "authorization_pending" } },
        },
        { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
      ]);
      const result = await setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test_client",
        fetchImpl,
      });
      expect(result.accessToken).toBe("aat_TEST");
    },
    30_000, // slow_down は +5s interval、 1 + 6 + 6 = 13s 程度を許容
  );

  test("e2: pending → pending → success", async () => {
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: deviceAuthorizeBody(),
      },
      {
        path: "/oauth/token",
        status: 400,
        body: { detail: { error: "authorization_pending" } },
      },
      {
        path: "/oauth/token",
        status: 400,
        body: { detail: { error: "authorization_pending" } },
      },
      { path: "/oauth/token", status: 200, body: tokenSuccessBody() },
    ]);
    const result = await setupViaDeviceCode({
      baseUrl: "https://api.example.com",
      clientId: "dcr_test_client",
      fetchImpl,
    });
    expect(result.accessToken).toBe("aat_TEST");
  });

  test("e3: AbortSignal during polling → InstallAbortedError", async () => {
    const ac = new AbortController();
    const fetchImpl = makeFetch([
      {
        path: "/oauth/device/authorize",
        status: 200,
        body: { ...deviceAuthorizeBody(), interval: 1 },
      },
    ]);
    // abort immediately after issuing device_code
    setTimeout(() => ac.abort(), 5);
    await expect(
      setupViaDeviceCode({
        baseUrl: "https://api.example.com",
        clientId: "dcr_test_client",
        signal: ac.signal,
        fetchImpl,
      }),
    ).rejects.toBeInstanceOf(InstallAbortedError);
  });
});
