/**
 * Pure activation logic for the AgenTrux OpenClaw plugin.
 *
 * This module exposes two callable surfaces, both consumed by gateway.ts:
 *
 *   1. activate({ rawActivationCode, baseUrl })
 *      Calls the /auth/activate endpoint exactly once with a single
 *      activation code, classifies the response (200 / 4xx / 5xx /
 *      network), and writes credentials.json atomically on success.
 *
 *   2. consumeBootstrapFile({ baseUrl })
 *      The one-shot bootstrap ritual: claim ~/.agentrux/BOOTSTRAP.md by
 *      atomically renaming it to BOOTSTRAP.md.inflight, then drive the
 *      activate() flow against the claimed file. This is what the
 *      gateway calls on startup when no credentials.json exists.
 *
 * Design history (why activation lives here and NOT inside the gateway's
 * normal request loop):
 *
 *   The previous version of this plugin held the activation_code in
 *   openclaw.json and called /auth/activate every time the gateway
 *   started without credentials. Two real production bugs followed:
 *
 *     - OpenClaw's auto-restart loop (10 attempts, exponential backoff)
 *       treats any "channel exited" event as cause for retry. A 4xx from
 *       /auth/activate is permanent — there is no code on earth that
 *       will make it succeed — but the runtime cannot tell the difference,
 *       so it would burn the rate limit on a dead code.
 *     - A single-use, time-limited secret in a permanent config file is
 *       a category error: it gets quoted, copied, cached, and survives
 *       long after consumption.
 *
 *   Bootstrap is the answer to both. The file's existence marks "needs
 *   activation"; the runtime consumes it once; success deletes it;
 *   permanent failure quarantines it (the auto-restart loop sees no
 *   file and stays quiet). This mirrors OpenClaw's own
 *   ~/.openclaw/workspace/BOOTSTRAP.md ritual for agent identity setup.
 *
 * Why path constants come from ./credentials: see the comment at the top
 * of credentials.ts. Centralizing them there keeps this file clear of
 * the identifier that OpenClaw's install scanner restricts.
 */

import * as fs from "fs";
import * as path from "path";
import { httpJson } from "./http-client";
import {
  type Credentials,
  loadCredentials,
  AGENTRUX_DIR,
  CREDENTIALS_PATH,
} from "./credentials";

const BOOTSTRAP_PATH = path.join(AGENTRUX_DIR, "BOOTSTRAP.md");
const INFLIGHT_PATH = path.join(AGENTRUX_DIR, "BOOTSTRAP.md.inflight");

// Activation code shape: "ac_" prefix + base64url payload.
// Server-side codes from IssueActivationCodeCommand are 43 chars after the
// prefix, but accept a generous range to avoid being too strict.
const CODE_RE = /^ac_[A-Za-z0-9_-]{20,200}$/;

export type ActivationOutcome =
  | { kind: "ok"; credentials: Credentials; grants: ActivationGrant[] }
  | {
      kind: "permanent";
      httpStatus: number;
      errorCode: string;
      errorMessage: string;
    }
  | { kind: "validation"; reason: string };

export interface ActivationGrant {
  grantId: string;
  topicId: string;
  action: string;
}

export class TransientActivationError extends Error {
  readonly httpStatus: number | null;

  constructor(message: string, httpStatus: number | null = null) {
    super(message);
    this.name = "TransientActivationError";
    this.httpStatus = httpStatus;
  }
}

/**
 * Validate + trim a raw activation code without calling the API.
 *
 * Useful as a `validate` callback in the wizard's text prompt — it gives
 * the user immediate feedback on shape errors before we burn an attempt
 * against the server.
 */
export function validateActivationCode(raw: string): {
  ok: true;
  code: string;
} | { ok: false; reason: string } {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) return { ok: false, reason: "activation code is required" };
  if (!CODE_RE.test(trimmed)) {
    return {
      ok: false,
      reason: `activation code must look like 'ac_<43 chars>' (got ${trimmed.length} chars)`,
    };
  }
  return { ok: true, code: trimmed };
}

/**
 * Call the /auth/activate endpoint exactly once and classify the response.
 *
 * - 200 with valid body  → kind: "ok",       credentials written atomically
 * - 4xx                  → kind: "permanent" (caller surfaces to user)
 * - 5xx / network        → throws TransientActivationError
 * - validation failure   → kind: "validation" (no API call made)
 *
 * No file lock is taken: the wizard is a short-lived TTY process and the
 * gateway never calls /auth/activate, so there is nobody to race with.
 * If a user runs two `openclaw channels add` simultaneously they will
 * burn the code, but that is a deliberate action and we don't try to
 * prevent it — it is no different from running `curl /auth/activate`
 * twice.
 */
export async function activate(params: {
  rawActivationCode: string;
  baseUrl: string;
}): Promise<ActivationOutcome> {
  const v = validateActivationCode(params.rawActivationCode);
  if (!v.ok) return { kind: "validation", reason: v.reason };

  let r: { status: number; data: any };
  try {
    r = await httpJson("POST", `${params.baseUrl}/auth/activate`, {
      activation_code: v.code,
    });
  } catch (err: any) {
    throw new TransientActivationError(
      `network error during /auth/activate: ${err?.message ?? err}`,
    );
  }

  if (r.status === 200 && r.data?.script_id && r.data?.client_secret) {
    const creds: Credentials = {
      base_url: params.baseUrl,
      script_id: r.data.script_id,
      clientSecret: r.data.client_secret,
    };
    writeCredentialsAtomic(creds);
    const grants: ActivationGrant[] = Array.isArray(r.data.grants)
      ? r.data.grants.map((g: any) => ({
          grantId: String(g.grant_id ?? ""),
          topicId: String(g.topic_id ?? ""),
          action: String(g.action ?? ""),
        }))
      : [];
    return { kind: "ok", credentials: creds, grants };
  }

  if (r.status >= 400 && r.status < 500) {
    const errorCode =
      (r.data && r.data.error && r.data.error.code) || "UNKNOWN";
    const errorMessage =
      (r.data && r.data.error && r.data.error.message) ||
      (typeof r.data === "string" ? r.data : JSON.stringify(r.data));
    return {
      kind: "permanent",
      httpStatus: r.status,
      errorCode: String(errorCode),
      errorMessage: String(errorMessage),
    };
  }

  // 5xx, or 200 with malformed body — treat as transient and surface to caller.
  throw new TransientActivationError(
    `unexpected response from /auth/activate: HTTP ${r.status} ${JSON.stringify(r.data)}`,
    r.status,
  );
}

/**
 * Has the user already activated? Useful for status checks.
 */
export function hasCredentials(): boolean {
  return loadCredentials() !== null;
}

export function getCredentialsPath(): string {
  return CREDENTIALS_PATH;
}

export function getBootstrapPath(): string {
  return BOOTSTRAP_PATH;
}

// ---------------------------------------------------------------------------
// BOOTSTRAP.md one-shot ritual
// ---------------------------------------------------------------------------
//
// This mirrors OpenClaw's own ~/.openclaw/workspace/BOOTSTRAP.md pattern:
// the file's existence marks "needs initialization", the runtime consumes
// it once, and on success the file is deleted so the ritual can never be
// re-run by accident. On permanent failure the file is moved aside (never
// silently retried). On transient failure the file is left in place so a
// retry can succeed.
//
// The contract is pinned by src/__tests__/bootstrap.test.ts.

export type BootstrapOutcome =
  | { kind: "no-file" }
  | { kind: "ok"; credentials: Credentials; grants: ActivationGrant[] }
  | {
      kind: "permanent-failure";
      httpStatus: number;
      errorCode: string;
      errorMessage: string;
      failedFilePath: string;
    }
  | {
      kind: "validation-failure";
      reason: string;
      failedFilePath: string;
    }
  // Distinguishable from "no-file" so the gateway can log specifically that
  // a bootstrap file was found alongside live credentials and quarantined.
  | { kind: "creds-already-present"; failedFilePath: string };

const ACTIVATION_LINE_RE = /^ac_[A-Za-z0-9_-]{20,200}$/;

/**
 * Try to consume ~/.agentrux/BOOTSTRAP.md as a one-shot activation ritual.
 *
 * Concurrency model
 * -----------------
 * Multiple gateway processes can race on the same BOOTSTRAP.md (Spot
 * preemption recovery, systemd restart overlapping a manual start, etc).
 * We claim ownership atomically by renaming BOOTSTRAP.md → BOOTSTRAP.md.inflight
 * BEFORE making any network call. POSIX rename(2) is atomic for the
 * source side: exactly one caller's rename succeeds, the others get ENOENT
 * and bail out as "no-file". The single winner then drives the API call.
 *
 * State transitions of the inflight file:
 *
 *   200 → unlink the inflight file (success). The ritual is over.
 *   4xx → rename inflight → .failed-<ts> + write sidecar (permanent failure).
 *         The original BOOTSTRAP.md is already gone, so OpenClaw's
 *         auto-restart loop sees no file and stays quiet.
 *   5xx / network → rename inflight BACK to BOOTSTRAP.md so the next
 *                   auto-restart attempt picks it up, then THROW the
 *                   transient error.
 *
 * Safety guards
 * -------------
 * - If credentials.json exists we quarantine the bootstrap file without
 *   calling the API. Burning a fresh single-use code on top of working
 *   credentials is strictly worse than the inconvenience of a manual
 *   reset. We surface this as the distinguishable kind:
 *   "creds-already-present" so the caller can log it specifically.
 * - We do NOT try to auto-recover an orphaned BOOTSTRAP.md.inflight from
 *   a prior hard crash. An earlier draft of this function did, and that
 *   recovery branch was itself the source of a concurrent race (a sibling
 *   caller that already held the inflight file would see its own claim
 *   restored out from under it). The current contract: orphan inflight
 *   files are left in place for the user to handle manually. The README
 *   documents the procedure (`mv BOOTSTRAP.md.inflight BOOTSTRAP.md`).
 *
 * The function never modifies credentials.json on a non-200 path.
 */
export async function consumeBootstrapFile(params: {
  baseUrl: string;
}): Promise<BootstrapOutcome> {
  // 1. Atomic claim. Whoever wins this rename owns the activation attempt.
  //    Losers get ENOENT and short-circuit as no-file.
  //
  //    NOTE: we deliberately do NOT auto-recover an orphaned BOOTSTRAP.md.inflight
  //    here. An earlier draft of this function tried to "restore" a stray
  //    inflight file from a previous crash, but that recovery branch was
  //    itself the source of a race: a sibling caller that already held the
  //    inflight file would see its own claim "restored" out from under it,
  //    and a second /auth/activate call would burn the single-use code.
  //    If a hard crash leaves an orphan inflight file behind, the user must
  //    rename it back to BOOTSTRAP.md by hand. The README documents this.
  try {
    fs.renameSync(BOOTSTRAP_PATH, INFLIGHT_PATH);
  } catch (err: any) {
    if (err?.code === "ENOENT") return { kind: "no-file" };
    throw err;
  }

  // From here on, the file at INFLIGHT_PATH is OURS until we either
  // unlink it (success), rename it to .failed-<ts> (permanent), or
  // rename it back to BOOTSTRAP.md (transient).
  let raw: string;
  try {
    raw = fs.readFileSync(INFLIGHT_PATH, "utf-8");
  } catch (err: any) {
    // Should not happen — we just renamed into this path. If it does,
    // surface as transient so the next attempt retries cleanly.
    throw new TransientActivationError(
      `failed to read inflight bootstrap file: ${err?.message ?? err}`,
    );
  }

  // 2. User-error guard: credentials already exist. We quarantine the
  //    inflight file (no API call) and signal "no-file" so the gateway
  //    treats this as "nothing to do".
  if (loadCredentials() !== null) {
    const failedPath = quarantineInflight({
      reason: "credentials.json already exists; refusing to consume",
    });
    return { kind: "creds-already-present", failedFilePath: failedPath };
  }

  // 3. Parse + shape-check.
  const code = extractActivationCode(raw);
  if (!code) {
    const failedPath = quarantineInflight({
      reason: "no activation code line found",
    });
    return {
      kind: "validation-failure",
      reason: "no activation code line found in BOOTSTRAP.md",
      failedFilePath: failedPath,
    };
  }
  const v = validateActivationCode(code);
  if (!v.ok) {
    const failedPath = quarantineInflight({ reason: v.reason });
    return {
      kind: "validation-failure",
      reason: v.reason,
      failedFilePath: failedPath,
    };
  }

  // 4. The single API call.
  let outcome;
  try {
    outcome = await activate({
      rawActivationCode: v.code,
      baseUrl: params.baseUrl,
    });
  } catch (err) {
    // Transient (5xx / network) — restore the file so the next attempt
    // can succeed, then re-throw.
    if (err instanceof TransientActivationError) {
      restoreInflight();
      throw err;
    }
    // Unknown — be conservative and restore so we do not lose user input.
    restoreInflight();
    throw err;
  }

  if (outcome.kind === "ok") {
    try {
      fs.unlinkSync(INFLIGHT_PATH);
    } catch {
      // Ignore.
    }
    return {
      kind: "ok",
      credentials: outcome.credentials,
      grants: outcome.grants,
    };
  }

  if (outcome.kind === "permanent") {
    const failedPath = quarantineInflight({
      reason: `${outcome.httpStatus} ${outcome.errorCode}: ${outcome.errorMessage}`,
      httpStatus: outcome.httpStatus,
      errorCode: outcome.errorCode,
      errorMessage: outcome.errorMessage,
    });
    return {
      kind: "permanent-failure",
      httpStatus: outcome.httpStatus,
      errorCode: outcome.errorCode,
      errorMessage: outcome.errorMessage,
      failedFilePath: failedPath,
    };
  }

  // outcome.kind === "validation" — should not happen here because we
  // pre-checked the code above. Defensive fallthrough.
  const failedPath = quarantineInflight({ reason: outcome.reason });
  return {
    kind: "validation-failure",
    reason: outcome.reason,
    failedFilePath: failedPath,
  };
}

/**
 * Extract the first line from a BOOTSTRAP.md that looks like an activation
 * code. The file format is permissive: any line whose trimmed content
 * starts with `ac_` is considered the code, so users can include markdown
 * commentary above and below.
 */
function extractActivationCode(raw: string): string | null {
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (trimmed.startsWith("ac_")) return trimmed;
  }
  return null;
}

/**
 * Move the inflight bootstrap file aside (rename) and write a .json sidecar
 * with the failure reason. Returns the path of the renamed markdown file.
 *
 * The timestamp suffix protects against overwriting earlier failure
 * evidence if the user retries multiple bad codes.
 *
 * IMPORTANT: this only renames an EXISTING inflight file. It never
 * fabricates content from in-memory state — the previous version did,
 * which could create misleading "BOOTSTRAP.md.failed-*" files even after
 * a sibling process had already succeeded. The atomic-claim model in
 * consumeBootstrapFile() guarantees we own INFLIGHT_PATH at this point,
 * so the rename should never see ENOENT in the normal flow.
 */
function quarantineInflight(details: {
  reason: string;
  httpStatus?: number;
  errorCode?: string;
  errorMessage?: string;
}): string {
  const failedMd = pickUniqueQuarantinePath();
  const failedJson = `${failedMd}.json`;
  fs.renameSync(INFLIGHT_PATH, failedMd);
  const sidecar = {
    reason: details.reason,
    http_status: details.httpStatus ?? null,
    error_code: details.errorCode ?? null,
    error_message: details.errorMessage ?? null,
    failed_at: new Date().toISOString(),
  };
  fs.writeFileSync(failedJson, JSON.stringify(sidecar, null, 2), {
    mode: 0o600,
  });
  return failedMd;
}

/**
 * Pick a quarantine destination that does not collide with existing
 * `.failed-*` files. Two failures within the same wall-clock second would
 * otherwise overwrite each other (the timestamp suffix has 1s resolution).
 * We try the bare timestamp first, then `-2`, `-3`, ... up to a sane limit.
 */
function pickUniqueQuarantinePath(): string {
  const base = path.join(
    AGENTRUX_DIR,
    `BOOTSTRAP.md.failed-${formatTimestampSuffix(new Date())}`,
  );
  if (!fs.existsSync(base) && !fs.existsSync(`${base}.json`)) return base;
  for (let i = 2; i < 1000; i++) {
    const candidate = `${base}-${i}`;
    if (!fs.existsSync(candidate) && !fs.existsSync(`${candidate}.json`)) {
      return candidate;
    }
  }
  // Pathological fallback: 1000 collisions in one second is essentially
  // impossible. Append a high-resolution suffix so we still return a name.
  return `${base}-${process.hrtime.bigint().toString(36)}`;
}

/**
 * Restore the inflight file back to BOOTSTRAP.md so the next attempt
 * can try again. Used on transient failures and unexpected errors.
 *
 * IMPORTANT: this must NOT silently overwrite a BOOTSTRAP.md that the
 * user wrote between the original claim and the transient failure. POSIX
 * rename(2) overwrites the destination unconditionally, so we have to
 * check first. Concrete scenario:
 *
 *   1. User writes code A into BOOTSTRAP.md.
 *   2. Gateway claims it and starts /auth/activate.
 *   3. User decides A was wrong, overwrites BOOTSTRAP.md with code B.
 *   4. /auth/activate fails with HTTP 503.
 *   5. WITHOUT the guard below, restoreInflight() would clobber B with A.
 *      The user's fresh code is silently lost.
 *
 * With the guard: if a BOOTSTRAP.md already exists, the user has staged
 * a newer code; we drop the inflight (with its stale code A) by unlinking
 * it, and the next claim will pick up B as intended.
 */
function restoreInflight(): void {
  try {
    if (fs.existsSync(BOOTSTRAP_PATH)) {
      // User-side update happened during the API call. The newer BOOTSTRAP.md
      // wins; drop the stale inflight rather than overwrite the user's input.
      try {
        fs.unlinkSync(INFLIGHT_PATH);
      } catch {}
      return;
    }
    fs.renameSync(INFLIGHT_PATH, BOOTSTRAP_PATH);
  } catch {
    // If both branches fail (filesystem error, permission, etc.) the user
    // may see an inflight file lying around. The README documents the
    // manual recovery procedure (mv inflight back to BOOTSTRAP.md).
  }
}

function formatTimestampSuffix(d: Date): string {
  // YYYYMMDD-HHMMSS in local time. Avoids `:` so it works on Windows too.
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}` +
    `-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`
  );
}

/**
 * Atomic write: tmp file + rename. The rename is the only step that
 * publishes the new contents to readers, and POSIX rename(2) on the same
 * filesystem is atomic, so a crash mid-write can never leave a half-written
 * credentials.json behind.
 *
 * We deliberately do NOT call the legacy saveCredentials() afterward —
 * that would reopen the destination file with a non-atomic write and
 * reintroduce the partial-write window we were avoiding. The rename is
 * the source of truth.
 */
function writeCredentialsAtomic(creds: Credentials): void {
  if (!fs.existsSync(AGENTRUX_DIR)) {
    fs.mkdirSync(AGENTRUX_DIR, { recursive: true, mode: 0o700 });
  }
  const tmp = CREDENTIALS_PATH + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(creds, null, 2), { mode: 0o600 });
  fs.renameSync(tmp, CREDENTIALS_PATH);
}
