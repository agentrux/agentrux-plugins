/**
 * BOOTSTRAP.md one-shot contract tests.
 *
 * Pins the lifecycle of the bootstrap file under ~/.agentrux/BOOTSTRAP.md.
 * The contract mirrors OpenClaw's own BOOTSTRAP.md ritual: a file exists
 * to mark "needs initialization", the runtime consumes it once, and the
 * file is deleted (success) or quarantined (permanent failure) so the
 * ritual can never be re-run by accident.
 *
 * State machine:
 *
 *   no file                       → no-file outcome, no I/O
 *   file + creds present          → quarantine the file (user error guard)
 *   file + valid + 200            → ok, write credentials, delete file
 *   file + valid + 4xx            → permanent-failure, rename to .failed-*
 *   file + valid + 5xx            → THROW (transient), file untouched
 *   file + valid + network error  → THROW (transient), file untouched
 *   file + malformed              → validation-failure, rename to .failed-*
 *
 * The "quarantine" rename uses a timestamp suffix so a user can fix and
 * retry without overwriting prior failure evidence.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

const httpJsonMock = jest.fn();
jest.mock("../http-client", () => ({
  httpJson: (...args: unknown[]) => httpJsonMock(...args),
}));

let tmpHome: string;
let agentruxDir: string;
let bootstrapPath: string;
let credentialsPath: string;

const baseUrl = "https://api.example.test";
const goodCode = "ac_" + "A".repeat(43);

beforeEach(() => {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "agentrux-bootstrap-"));
  process.env.HOME = tmpHome;
  agentruxDir = path.join(tmpHome, ".agentrux");
  bootstrapPath = path.join(agentruxDir, "BOOTSTRAP.md");
  credentialsPath = path.join(agentruxDir, "credentials.json");
  fs.mkdirSync(agentruxDir, { recursive: true, mode: 0o700 });
  httpJsonMock.mockReset();
  jest.resetModules();
});

afterEach(() => {
  fs.rmSync(tmpHome, { recursive: true, force: true });
});

function writeBootstrap(content: string): void {
  fs.writeFileSync(bootstrapPath, content, { mode: 0o600 });
}

function listFailedFiles(): string[] {
  return fs
    .readdirSync(agentruxDir)
    .filter((n) => n.startsWith("BOOTSTRAP.md.failed-"));
}

function loadModule() {
  return require("../activation-core") as typeof import("../activation-core");
}

describe("consumeBootstrapFile — no file", () => {
  test("no BOOTSTRAP.md → no-file outcome, no API call, no file changes", async () => {
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("no-file");
    expect(httpJsonMock).not.toHaveBeenCalled();
    expect(fs.existsSync(bootstrapPath)).toBe(false);
    expect(fs.existsSync(credentialsPath)).toBe(false);
  });
});

describe("consumeBootstrapFile — happy path", () => {
  test("file + 200 → ok, credentials written, BOOTSTRAP.md deleted", async () => {
    writeBootstrap(goodCode + "\n");
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: {
        script_id: "scr_1",
        client_secret: "secret_xyz",
        grants: [{ grant_id: "g1", topic_id: "t1", action: "read" }],
      },
    });

    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });

    expect(out.kind).toBe("ok");
    if (out.kind !== "ok") return;
    expect(out.credentials.script_id).toBe("scr_1");
    expect(out.credentials.clientSecret).toBe("secret_xyz");
    expect(out.grants).toHaveLength(1);

    expect(fs.existsSync(bootstrapPath)).toBe(false); // deleted
    expect(fs.existsSync(credentialsPath)).toBe(true); // written
    const onDisk = JSON.parse(fs.readFileSync(credentialsPath, "utf-8"));
    expect(onDisk.script_id).toBe("scr_1");
    expect(httpJsonMock).toHaveBeenCalledTimes(1);
  });

  test("file with surrounding markdown comments → code is extracted", async () => {
    // BOOTSTRAP.md format is permissive: any line that starts with `ac_`
    // counts as the activation code. Users can put commentary above or below.
    writeBootstrap(
      [
        "# AgenTrux activation",
        "",
        "Paste your single-use code below; this file will be deleted on success.",
        "",
        goodCode,
        "",
        "(do not edit anything else)",
      ].join("\n"),
    );
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_2", client_secret: "s" },
    });

    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("ok");
    expect(httpJsonMock).toHaveBeenCalledWith(
      "POST",
      `${baseUrl}/auth/activate`,
      { activation_code: goodCode },
    );
  });

  test("file with leading whitespace and trailing newline → code is trimmed", async () => {
    writeBootstrap(`  \n  ${goodCode}  \n  \n`);
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_3", client_secret: "s" },
    });
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("ok");
    expect(httpJsonMock).toHaveBeenCalledWith(
      "POST",
      `${baseUrl}/auth/activate`,
      { activation_code: goodCode },
    );
  });
});

describe("consumeBootstrapFile — credentials already exist (user-error guard)", () => {
  test("file + creds present → quarantine, distinguishable creds-already-present outcome", async () => {
    // Pre-seed credentials.
    fs.writeFileSync(
      credentialsPath,
      JSON.stringify({
        base_url: baseUrl,
        script_id: "scr_existing",
        clientSecret: "old",
      }),
    );
    writeBootstrap(goodCode);

    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });

    // We do NOT consume the code: there is nothing for it to do, and burning
    // a single-use code by mistake is the worst possible outcome.
    expect(httpJsonMock).not.toHaveBeenCalled();
    // The bootstrap file is moved aside so the gateway does not keep
    // tripping on it on every restart.
    expect(fs.existsSync(bootstrapPath)).toBe(false);
    expect(listFailedFiles().length).toBeGreaterThan(0);
    // The outcome is distinguishable from a plain no-file so the gateway
    // can log this case specifically (Codex review feedback).
    expect(out.kind).toBe("creds-already-present");
    if (out.kind === "creds-already-present") {
      expect(out.failedFilePath).toMatch(/BOOTSTRAP\.md\.failed-/);
    }
    // Existing credentials are untouched.
    const stillThere = JSON.parse(fs.readFileSync(credentialsPath, "utf-8"));
    expect(stillThere.script_id).toBe("scr_existing");
  });
});

describe("consumeBootstrapFile — permanent failure (4xx)", () => {
  test("file + 404 → permanent-failure, file renamed, .json sidecar written", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: { error: { code: "NOT_FOUND", message: "Activation code not found" } },
    });

    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });

    expect(out.kind).toBe("permanent-failure");
    if (out.kind !== "permanent-failure") return;
    expect(out.httpStatus).toBe(404);
    expect(out.errorCode).toBe("NOT_FOUND");

    // Original file is gone — auto-restart loop must not retry.
    expect(fs.existsSync(bootstrapPath)).toBe(false);
    // Quarantine file present.
    const failed = listFailedFiles();
    expect(failed.length).toBe(2); // .failed-* and .failed-*.json
    const md = failed.find((n) => !n.endsWith(".json"))!;
    const jsonName = failed.find((n) => n.endsWith(".json"))!;
    expect(jsonName.startsWith(md)).toBe(true);
    // Sidecar contains diagnostic info.
    const sidecar = JSON.parse(
      fs.readFileSync(path.join(agentruxDir, jsonName), "utf-8"),
    );
    expect(sidecar.http_status).toBe(404);
    expect(sidecar.error_code).toBe("NOT_FOUND");
    // No credentials written.
    expect(fs.existsSync(credentialsPath)).toBe(false);
  });

  test("file + 422 expired → permanent-failure", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 422,
      data: { error: { code: "INVALID", message: "Token has expired" } },
    });

    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });

    expect(out.kind).toBe("permanent-failure");
    if (out.kind !== "permanent-failure") return;
    expect(out.httpStatus).toBe(422);
    expect(out.errorCode).toBe("INVALID");
    expect(fs.existsSync(bootstrapPath)).toBe(false);
  });
});

describe("consumeBootstrapFile — transient failure (5xx / network)", () => {
  test("file + 503 → THROW transient, file UNTOUCHED so a retry can succeed", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 503,
      data: { error: { code: "UNAVAILABLE" } },
    });

    const { consumeBootstrapFile, TransientActivationError } = loadModule();
    await expect(
      consumeBootstrapFile({ baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    // The file MUST stay so that the next gateway restart picks it up.
    expect(fs.existsSync(bootstrapPath)).toBe(true);
    expect(listFailedFiles()).toEqual([]);
    expect(fs.existsSync(credentialsPath)).toBe(false);
  });

  test("file + network error → THROW transient, file UNTOUCHED", async () => {
    writeBootstrap(goodCode);
    const netErr: any = new Error("connect ECONNREFUSED");
    netErr.code = "ECONNREFUSED";
    httpJsonMock.mockRejectedValueOnce(netErr);

    const { consumeBootstrapFile, TransientActivationError } = loadModule();
    await expect(
      consumeBootstrapFile({ baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    expect(fs.existsSync(bootstrapPath)).toBe(true);
  });
});

describe("consumeBootstrapFile — validation failure", () => {
  test("file with no recognizable code → validation-failure, file quarantined", async () => {
    writeBootstrap("this is not an activation code\nhello world\n");
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });

    expect(out.kind).toBe("validation-failure");
    if (out.kind !== "validation-failure") return;
    expect(out.reason).toMatch(/no.*activation code/i);

    expect(httpJsonMock).not.toHaveBeenCalled();
    expect(fs.existsSync(bootstrapPath)).toBe(false);
    expect(listFailedFiles().length).toBeGreaterThan(0);
  });

  test("file with malformed ac_ line → validation-failure", async () => {
    writeBootstrap("ac_short\n");
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("validation-failure");
    expect(httpJsonMock).not.toHaveBeenCalled();
    expect(fs.existsSync(bootstrapPath)).toBe(false);
  });

  test("empty file → validation-failure", async () => {
    writeBootstrap("");
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("validation-failure");
    expect(fs.existsSync(bootstrapPath)).toBe(false);
  });
});

describe("consumeBootstrapFile — concurrent claim (Codex MUST-FIX)", () => {
  test("two concurrent callers race on the same file → ONLY ONE calls /auth/activate", async () => {
    // Setup: one BOOTSTRAP.md, no credentials. Two parallel callers will
    // both try to consume it. The atomic rename guarantees only one of
    // them can ever reach the API call.
    writeBootstrap(goodCode);
    httpJsonMock.mockImplementation(async () => {
      // Add a tiny async delay so both promises are definitely in flight
      // when the rename race happens.
      await new Promise((r) => setImmediate(r));
      return {
        status: 200,
        data: { script_id: "scr_winner", client_secret: "secret" },
      };
    });

    const { consumeBootstrapFile } = loadModule();
    const [a, b] = await Promise.all([
      consumeBootstrapFile({ baseUrl }),
      consumeBootstrapFile({ baseUrl }),
    ]);

    // Exactly one /auth/activate call. This is the contract Codex pinned.
    expect(httpJsonMock).toHaveBeenCalledTimes(1);

    // Exactly one "ok" outcome.
    const oks = [a, b].filter((o) => o.kind === "ok");
    const noFiles = [a, b].filter((o) => o.kind === "no-file");
    expect(oks.length).toBe(1);
    expect(noFiles.length).toBe(1);

    // The winner published credentials atomically.
    expect(fs.existsSync(credentialsPath)).toBe(true);
    const written = JSON.parse(fs.readFileSync(credentialsPath, "utf-8"));
    expect(written.script_id).toBe("scr_winner");

    // The original BOOTSTRAP.md and its inflight twin are both gone —
    // no leftover state for a future run to trip over.
    expect(fs.existsSync(bootstrapPath)).toBe(false);
    expect(
      fs.existsSync(path.join(agentruxDir, "BOOTSTRAP.md.inflight")),
    ).toBe(false);

    // No spurious .failed-* file from the loser fabricating one.
    expect(listFailedFiles()).toEqual([]);
  });

  test("loser does NOT fabricate a .failed-* file (Codex MUST-FIX)", async () => {
    // Specifically pin the second half of the bug Codex flagged: the
    // previous quarantine() implementation could fall through to a
    // writeFileSync() that synthesised a `.failed-*` from in-memory raw
    // content, even after the winner had already deleted BOOTSTRAP.md.
    // The new implementation must NEVER do that.
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValue({
      status: 200,
      data: { script_id: "scr_winner", client_secret: "s" },
    });
    const { consumeBootstrapFile } = loadModule();
    await Promise.all([
      consumeBootstrapFile({ baseUrl }),
      consumeBootstrapFile({ baseUrl }),
      consumeBootstrapFile({ baseUrl }),
    ]);
    expect(listFailedFiles()).toEqual([]);
    expect(httpJsonMock).toHaveBeenCalledTimes(1);
  });

  test("transient failure restores the file so the next call can retry", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 503,
      data: { error: { code: "UNAVAILABLE" } },
    });
    const { consumeBootstrapFile, TransientActivationError } = loadModule();
    await expect(
      consumeBootstrapFile({ baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    // The inflight name should not leak after a transient failure — the
    // file must be back at BOOTSTRAP.md so the next attempt can claim it.
    expect(
      fs.existsSync(path.join(agentruxDir, "BOOTSTRAP.md.inflight")),
    ).toBe(false);
    expect(fs.existsSync(bootstrapPath)).toBe(true);

    // Second call: same code path, succeeds.
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_late", client_secret: "s" },
    });
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("ok");
    expect(httpJsonMock).toHaveBeenCalledTimes(2);
  });

  test("transient failure must NOT overwrite a fresh BOOTSTRAP.md the user wrote during the API call (Codex 2nd-round MUST-FIX)", async () => {
    // Scenario: user writes code A, gateway claims it and starts the API
    // call, user notices A was wrong and writes code B over BOOTSTRAP.md
    // while the call is in flight, then the call fails with 5xx. The
    // restore step MUST NOT clobber B with A.
    const codeA = "ac_" + "A".repeat(43);
    const codeB = "ac_" + "B".repeat(43);
    writeBootstrap(codeA);

    // Mock the API call so we can write B between the claim and the
    // failure response.
    httpJsonMock.mockImplementationOnce(async () => {
      // The plugin has already claimed BOOTSTRAP.md → BOOTSTRAP.md.inflight.
      // Simulate the user racing in to replace BOOTSTRAP.md with the new code.
      writeBootstrap(codeB);
      return { status: 503, data: { error: { code: "UNAVAILABLE" } } };
    });

    const { consumeBootstrapFile, TransientActivationError } = loadModule();
    await expect(
      consumeBootstrapFile({ baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    // The fresh code B must still be at BOOTSTRAP.md, NOT overwritten by A.
    expect(fs.existsSync(bootstrapPath)).toBe(true);
    expect(fs.readFileSync(bootstrapPath, "utf-8")).toContain(codeB);
    expect(fs.readFileSync(bootstrapPath, "utf-8")).not.toContain(codeA);

    // The stale inflight (containing A) must be gone — we dropped it
    // because the user staged a newer code.
    expect(
      fs.existsSync(path.join(agentruxDir, "BOOTSTRAP.md.inflight")),
    ).toBe(false);

    // Second call picks up B and succeeds.
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_b", client_secret: "s" },
    });
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("ok");
    // The API was called with B, never with the stale A again.
    const calledWithB = httpJsonMock.mock.calls.some(
      (call) =>
        call[2] &&
        (call[2] as any).activation_code === codeB,
    );
    expect(calledWithB).toBe(true);
  });

  test("quarantine within the same second produces non-colliding filenames (Codex NICE-TO-HAVE)", async () => {
    // Two failures in the same wall-clock second must not overwrite each
    // other's evidence. We exercise this by quarantining twice back-to-back
    // and checking we end up with two distinct .failed-* files (and two
    // .json sidecars).
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: { error: { code: "NOT_FOUND", message: "first" } },
    });
    const { consumeBootstrapFile } = loadModule();
    const first = await consumeBootstrapFile({ baseUrl });
    expect(first.kind).toBe("permanent-failure");

    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: { error: { code: "NOT_FOUND", message: "second" } },
    });
    const second = await consumeBootstrapFile({ baseUrl });
    expect(second.kind).toBe("permanent-failure");

    const failed = listFailedFiles().filter((n) => !n.endsWith(".json"));
    expect(failed.length).toBe(2);
    // The two filenames must be distinct.
    expect(failed[0]).not.toBe(failed[1]);

    // Both .json sidecars must exist with their distinct contents.
    const jsonFiles = listFailedFiles().filter((n) => n.endsWith(".json"));
    expect(jsonFiles.length).toBe(2);
    const messages = jsonFiles
      .map((n) =>
        JSON.parse(fs.readFileSync(path.join(agentruxDir, n), "utf-8")),
      )
      .map((s) => s.error_message)
      .sort();
    expect(messages).toEqual(["first", "second"]);
  });

  test("orphaned .inflight without BOOTSTRAP.md → no-file (manual recovery, never auto-restore)", async () => {
    // An earlier draft of consumeBootstrapFile() tried to auto-recover a
    // stray inflight file by renaming it back to BOOTSTRAP.md before the
    // claim. That recovery branch was the source of the concurrent race:
    // a sibling caller that already held the inflight file would see its
    // own claim "restored" out from under it, and a second /auth/activate
    // call would burn the single-use code.
    //
    // The current contract: if a crash leaves an orphan .inflight behind,
    // we do NOT touch it. The function returns no-file. The user's
    // recovery procedure is documented in the README ("rename it back
    // to BOOTSTRAP.md by hand"). This is a deliberate trade: we lose
    // automatic crash recovery to gain race-free concurrent claim.
    fs.writeFileSync(
      path.join(agentruxDir, "BOOTSTRAP.md.inflight"),
      goodCode + "\n",
      { mode: 0o600 },
    );
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("no-file");
    expect(httpJsonMock).not.toHaveBeenCalled();
    // The orphan inflight is left in place for the user to inspect.
    expect(
      fs.existsSync(path.join(agentruxDir, "BOOTSTRAP.md.inflight")),
    ).toBe(true);
  });

  test("orphaned .inflight + live BOOTSTRAP.md → orphan is overwritten by the live claim (auto-cleanup)", async () => {
    // POSIX rename(2) overwrites the destination. So if a crash left an
    // orphan inflight behind AND the user wrote a fresh BOOTSTRAP.md, the
    // next claim renames the live file on top of the orphan and consumes
    // the new code. The stale orphan content is silently dropped.
    //
    // This is the intended behavior: an orphan inflight is a transient
    // crash artifact, and the user's freshly-written code is the source
    // of truth. The contract is "the live BOOTSTRAP.md wins".
    const orphanCode = "ac_" + "Z".repeat(43);
    fs.writeFileSync(
      path.join(agentruxDir, "BOOTSTRAP.md.inflight"),
      orphanCode + "\n",
      { mode: 0o600 },
    );
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_live", client_secret: "s" },
    });
    const { consumeBootstrapFile } = loadModule();
    const out = await consumeBootstrapFile({ baseUrl });
    expect(out.kind).toBe("ok");
    // The API was called with the LIVE code, not the orphan content.
    expect(httpJsonMock).toHaveBeenCalledWith(
      "POST",
      `${baseUrl}/auth/activate`,
      { activation_code: goodCode },
    );
    expect(httpJsonMock).not.toHaveBeenCalledWith(
      "POST",
      `${baseUrl}/auth/activate`,
      { activation_code: orphanCode },
    );
    // The orphan is gone (overwritten by the live claim and then deleted
    // on success).
    expect(
      fs.existsSync(path.join(agentruxDir, "BOOTSTRAP.md.inflight")),
    ).toBe(false);
  });
});

describe("consumeBootstrapFile — one-shot guarantee", () => {
  test("calling twice in a row after success is a no-op (file already gone)", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_1", client_secret: "s" },
    });
    const { consumeBootstrapFile } = loadModule();

    const first = await consumeBootstrapFile({ baseUrl });
    expect(first.kind).toBe("ok");

    // Second call: file is gone, AND credentials now exist. Both branches
    // skip the API call. The user-error guard does NOT trip because the
    // bootstrap file no longer exists.
    const second = await consumeBootstrapFile({ baseUrl });
    expect(second.kind).toBe("no-file");
    expect(httpJsonMock).toHaveBeenCalledTimes(1); // only the first call
  });

  test("calling twice in a row after permanent failure does NOT retry (file renamed)", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 404,
      data: { error: { code: "NOT_FOUND", message: "x" } },
    });
    const { consumeBootstrapFile } = loadModule();

    const first = await consumeBootstrapFile({ baseUrl });
    expect(first.kind).toBe("permanent-failure");

    // Second call: original file is gone, no API call.
    const second = await consumeBootstrapFile({ baseUrl });
    expect(second.kind).toBe("no-file");
    expect(httpJsonMock).toHaveBeenCalledTimes(1);
  });

  test("calling twice in a row after transient failure DOES retry (file still there)", async () => {
    writeBootstrap(goodCode);
    httpJsonMock.mockResolvedValueOnce({
      status: 503,
      data: { error: { code: "UNAVAILABLE" } },
    });
    const { consumeBootstrapFile, TransientActivationError } = loadModule();

    await expect(
      consumeBootstrapFile({ baseUrl }),
    ).rejects.toBeInstanceOf(TransientActivationError);

    // Second call: file is still there, second attempt succeeds.
    httpJsonMock.mockResolvedValueOnce({
      status: 200,
      data: { script_id: "scr_late", client_secret: "s" },
    });
    const second = await consumeBootstrapFile({ baseUrl });
    expect(second.kind).toBe("ok");
    expect(httpJsonMock).toHaveBeenCalledTimes(2);
  });
});
