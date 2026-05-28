/**
 * activation-core tests.
 *
 * activation-core is the pure side of the activation flow: it takes a raw
 * code string and a base URL, calls /auth/redeem-activation-code exactly
 * once (Phase 1.9+), classifies the response, and writes credentials
 * atomically on success. The setup wizard is what calls it.
 *
 * Successful response shape (per activation_code_router.py):
 *   {client_id: "crd_<uuid>", client_secret: "aks_<plain>",
 *    script_id: "scr_<uuid>", issued_at: "..."}
 *
 * We isolate from the real filesystem by pointing HOME at a tmpdir, and we
 * stub http-client.httpJson before importing the module under test.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const httpJsonMock = jest.fn();
jest.mock("../http-client", () => ({
  httpJson: (...args: unknown[]) => httpJsonMock(...args),
}));

let tmpHome: string;

beforeEach(() => {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "agentrux-actcore-"));
  process.env.HOME = tmpHome;
  httpJsonMock.mockReset();
  jest.resetModules();
});

afterEach(() => {
  fs.rmSync(tmpHome, { recursive: true, force: true });
});

function loadModule() {
  return require("../activation-core") as typeof import("../activation-core");
}

describe("validateActivationCode", () => {
  test("rejects empty input", () => {
    const { validateActivationCode } = loadModule();
    const r = validateActivationCode("");
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.reason).toMatch(/required/);
  });

  test("rejects garbage", () => {
    const { validateActivationCode } = loadModule();
    const r = validateActivationCode("hello world");
    expect(r.ok).toBe(false);
  });

  test("rejects code without act_ prefix", () => {
    const { validateActivationCode } = loadModule();
    const r = validateActivationCode("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA");
    expect(r.ok).toBe(false);
  });

  test("trims surrounding whitespace and newlines", () => {
    const { validateActivationCode } = loadModule();
    const code = "act_" + "A".repeat(43);
    const r = validateActivationCode(`\n  ${code}\t\n`);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.code).toBe(code);
  });

  test("accepts a valid code", () => {
    const { validateActivationCode } = loadModule();
    const code = "act_sj9hHesuy1YROE6PKz6ERmsnPEJ6iZp4bjXKWd-ABFI"; // 46 chars
    const r = validateActivationCode(code);
    expect(r.ok).toBe(true);
  });
});

describe("activate()", () => {
  const baseUrl = "https://api.example.test";
  const goodCode = "act_" + "A".repeat(43);

  test("ok: 200 response writes credentials atomically and returns ok", async () => {
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: {
        client_id: "crd_1",
        client_secret: "aks_secret_xyz",
        script_id: "scr_1",
      },
    });
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: goodCode, baseUrl });

    expect(out.kind).toBe("ok");
    if (out.kind !== "ok") return;
    expect(out.credentials.client_id).toBe("crd_1");
    expect(out.credentials.client_secret).toBe("aks_secret_xyz");
    expect(out.credentials.script_id).toBe("scr_1");
    expect(out.credentials.base_url).toBe(baseUrl);
    // /auth/redeem-activation-code does not return grants.
    expect(out.grants).toEqual([]);

    const onDisk = JSON.parse(
      fs.readFileSync(
        path.join(tmpHome, ".agentrux", "credentials.json"),
        "utf-8",
      ),
    );
    expect(onDisk.client_id).toBe("crd_1");
    expect(onDisk.client_secret).toBe("aks_secret_xyz");
    expect(onDisk.script_id).toBe("scr_1");
    expect(httpJsonMock).toHaveBeenCalledTimes(1);
  });

  test("trim: surrounding whitespace is stripped before sending", async () => {
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { client_id: "crd_2", client_secret: "aks_s", script_id: "scr_2" },
    });
    const { activate } = loadModule();
    const out = await activate({
      rawActivationCode: `  ${goodCode}\n`,
      baseUrl,
    });
    expect(out.kind).toBe("ok");
    expect(httpJsonMock).toHaveBeenCalledWith(
      "POST",
      `${baseUrl}/auth/redeem-activation-code`,
      { code: goodCode },
    );
  });

  test("validation: malformed code does NOT call the API", async () => {
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: "garbage", baseUrl });
    expect(out.kind).toBe("validation");
    expect(httpJsonMock).not.toHaveBeenCalled();
  });

  test("validation: empty code returns validation kind", async () => {
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: "", baseUrl });
    expect(out.kind).toBe("validation");
    expect(httpJsonMock).not.toHaveBeenCalled();
  });

  test("permanent: 404 returns permanent (no sentinel — wizard owns retry decision)", async () => {
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: {
        error: { code: "NOT_FOUND", message: "Activation code not found" },
      },
    });
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: goodCode, baseUrl });

    expect(out.kind).toBe("permanent");
    if (out.kind !== "permanent") return;
    expect(out.httpStatus).toBe(404);
    expect(out.errorCode).toBe("NOT_FOUND");
    expect(out.errorMessage).toContain("not found");

    expect(
      fs.existsSync(path.join(tmpHome, ".agentrux", "credentials.json")),
    ).toBe(false);
  });

  test("permanent: 4xx without an error envelope still classifies as permanent", async () => {
    // Raw string body falls back to the HTTP-status → code map (400 → INVALID).
    httpJsonMock.mockResolvedValueOnce({ status: 400, data: "Bad Request" });
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: goodCode, baseUrl });
    expect(out.kind).toBe("permanent");
    if (out.kind !== "permanent") return;
    expect(out.errorCode).toBe("INVALID");
    expect(out.errorMessage).toBe("Bad Request");
  });

  test("permanent: FastAPI HTTPException {detail: 'msg'} is parsed correctly", async () => {
    // /auth/redeem-activation-code uses HTTPException(detail=str) for 404/409/422.
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: { detail: "activation_code not found" },
    });
    const { activate } = loadModule();
    const out = await activate({ rawActivationCode: goodCode, baseUrl });
    expect(out.kind).toBe("permanent");
    if (out.kind !== "permanent") return;
    expect(out.httpStatus).toBe(404);
    expect(out.errorCode).toBe("NOT_FOUND");
    expect(out.errorMessage).toBe("activation_code not found");
  });

  test("transient: 5xx throws TransientActivationError", async () => {
    httpJsonMock.mockResolvedValueOnce({
      status: 503,
      data: { error: { code: "UNAVAILABLE", message: "try later" } },
    });
    const { activate, TransientActivationError } = loadModule();
    await expect(
      activate({ rawActivationCode: goodCode, baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    expect(
      fs.existsSync(path.join(tmpHome, ".agentrux", "credentials.json")),
    ).toBe(false);
  });

  test("transient: network error throws TransientActivationError", async () => {
    const netErr: any = new Error("connect ECONNREFUSED");
    netErr.code = "ECONNREFUSED";
    httpJsonMock.mockRejectedValueOnce(netErr);
    const { activate, TransientActivationError } = loadModule();
    await expect(
      activate({ rawActivationCode: goodCode, baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);
  });

  test("200 with missing client_id is treated as transient (NOT a successful activation)", async () => {
    // The server replied 200 but the body is missing required fields. The
    // critical invariant is that we MUST NOT write a half-formed credentials
    // file. Throwing transient is safer than silently writing junk.
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { client_secret: "aks_only" },
    });
    const { activate, TransientActivationError } = loadModule();
    await expect(
      activate({ rawActivationCode: goodCode, baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    expect(
      fs.existsSync(path.join(tmpHome, ".agentrux", "credentials.json")),
    ).toBe(false);
  });

  test("hasCredentials reflects on-disk state", async () => {
    const { hasCredentials, activate } = loadModule();
    expect(hasCredentials()).toBe(false);

    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { client_id: "crd_h", client_secret: "aks_s", script_id: "scr_h" },
    });
    await activate({ rawActivationCode: goodCode, baseUrl });
    expect(hasCredentials()).toBe(true);
  });

  test("getCredentialsPath returns the expected path under HOME", () => {
    const { getCredentialsPath } = loadModule();
    expect(getCredentialsPath()).toBe(
      path.join(tmpHome, ".agentrux", "credentials.json"),
    );
  });
});
