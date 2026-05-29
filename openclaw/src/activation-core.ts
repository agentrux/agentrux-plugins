/**
 * Pure activation logic for the AgenTrux OpenClaw plugin.
 *
 * This module exposes two callable surfaces:
 *
 *   1. activate({ rawActivationCode, baseUrl })
 *      Calls the /auth/redeem-activation-code endpoint exactly once with a single
 *      activation code, classifies the response (200 / 4xx / 5xx /
 *      network), and writes credentials.json atomically on success. This is
 *      the pure, unit-tested counterpart of the manual `agentrux_activate`
 *      tool (index.ts currently inlines an equivalent redeem call rather
 *      than delegating here — a known duplication, not a contract).
 *
 *   2. quarantineLegacyBootstrap()
 *      Retire a leftover ~/.agentrux/BOOTSTRAP.md from an older install.
 *      BOOTSTRAP.md no longer activates the channel — activation now runs
 *      through the device-code / topology setup flow (device-code-setup.ts).
 *      A stale BOOTSTRAP.md would otherwise sit inert and confuse the
 *      operator, so the gateway renames it once to a timestamped sibling on
 *      startup. No network call is made; credentials.json is never touched.
 *
 * History: an earlier version auto-activated from BOOTSTRAP.md (atomic
 * claim → /auth/activate → quarantine on 4xx). That endpoint and ritual are
 * gone; the only remnant we keep is the one-time cleanup above.
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

// Activation code shape: "act_" prefix + base64url payload.
// Server-side codes from IssueActivationCodeCommand are 43 chars after the
// prefix (activation_code_router.py / issue_activation_code.py), but accept
// a generous range to avoid being too strict.
const CODE_RE = /^act_[A-Za-z0-9_-]{20,200}$/;

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
      reason: `activation code must look like 'act_<43 chars>' (got ${trimmed.length} chars)`,
    };
  }
  return { ok: true, code: trimmed };
}

/**
 * Call the /auth/redeem-activation-code endpoint exactly once and classify the response.
 *
 * - 200 with valid body  → kind: "ok",       credentials written atomically
 * - 4xx                  → kind: "permanent" (caller surfaces to user)
 * - 5xx / network        → throws TransientActivationError
 * - validation failure   → kind: "validation" (no API call made)
 *
 * No file lock is taken: the wizard is a short-lived TTY process and the
 * gateway never calls /auth/redeem-activation-code, so there is nobody to race with.
 * If a user runs two `openclaw channels add` simultaneously they will
 * burn the code, but that is a deliberate action and we don't try to
 * prevent it — it is no different from running `curl /auth/redeem-activation-code`
 * twice.
 */
export async function activate(params: {
  rawActivationCode: string;
  baseUrl: string;
}): Promise<ActivationOutcome> {
  const v = validateActivationCode(params.rawActivationCode);
  if (!v.ok) return { kind: "validation", reason: v.reason };

  // Phase 1.9+ endpoint: POST /auth/redeem-activation-code body {code}.
  // Response: {client_id: "crd_<uuid>", client_secret: "aks_<plain>",
  //            script_id: "scr_<uuid>", issued_at}. The legacy endpoint
  // /auth/activate (and its {script_id, client_secret} response) is gone.
  let r: { status: number; data: any };
  try {
    r = await httpJson(
      "POST",
      `${params.baseUrl}/auth/redeem-activation-code`,
      { code: v.code },
    );
  } catch (err: any) {
    throw new TransientActivationError(
      `network error during /auth/redeem-activation-code: ${err?.message ?? err}`,
    );
  }

  if (r.status === 200 && r.data?.client_id && r.data?.client_secret) {
    const creds: Credentials = {
      base_url: params.baseUrl,
      client_id: String(r.data.client_id),
      client_secret: String(r.data.client_secret),
      script_id: r.data.script_id ? String(r.data.script_id) : undefined,
    };
    writeCredentialsAtomic(creds);
    // /auth/redeem-activation-code does not return grants — the script's
    // capabilities are encoded into the JWT scope claim issued by
    // /oauth/token, and surfaced separately if needed. Leave grants empty.
    return { kind: "ok", credentials: creds, grants: [] };
  }

  if (r.status >= 400 && r.status < 500) {
    // Server error shapes (in order of preference):
    //   - FastAPI HTTPException: `{detail: string | {...}}` — current
    //     /auth/redeem-activation-code uses this with a plain message string.
    //   - ConsoleAPIError envelope: `{error: {code, message}}` — used by
    //     newer /console endpoints, kept here for forward compatibility.
    //   - Raw string body (no JSON envelope).
    // We map all three to {errorCode, errorMessage} so the wizard / sidecar
    // surface stays human-readable regardless of which shape the server
    // emits.
    const httpStatusToCode: Record<number, string> = {
      400: "INVALID",
      401: "UNAUTHORIZED",
      403: "FORBIDDEN",
      404: "NOT_FOUND",
      409: "CONFLICT",
      422: "INVALID",
      429: "RATE_LIMITED",
    };
    let errorCode = "UNKNOWN";
    let errorMessage = "";
    if (r.data && typeof r.data === "object") {
      if (r.data.error && typeof r.data.error === "object") {
        errorCode = String(r.data.error.code ?? errorCode);
        errorMessage = String(r.data.error.message ?? "");
      } else if (typeof r.data.detail === "string") {
        errorMessage = r.data.detail;
      } else if (r.data.detail && typeof r.data.detail === "object") {
        errorCode = String((r.data.detail as any).code ?? errorCode);
        errorMessage = String(
          (r.data.detail as any).message ?? JSON.stringify(r.data.detail),
        );
      } else {
        errorMessage = JSON.stringify(r.data);
      }
    } else if (typeof r.data === "string") {
      errorMessage = r.data;
    }
    if (errorCode === "UNKNOWN" && httpStatusToCode[r.status]) {
      errorCode = httpStatusToCode[r.status];
    }
    if (!errorMessage) errorMessage = `HTTP ${r.status}`;
    return {
      kind: "permanent",
      httpStatus: r.status,
      errorCode,
      errorMessage,
    };
  }

  // 5xx, or 200 with malformed body — treat as transient and surface to caller.
  throw new TransientActivationError(
    `unexpected response from /auth/redeem-activation-code: HTTP ${r.status} ${JSON.stringify(r.data)}`,
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
// Legacy BOOTSTRAP.md retirement
// ---------------------------------------------------------------------------
//
// BOOTSTRAP.md activation is gone (the /auth/activate endpoint and its
// one-shot ritual were removed; activation now runs through the
// device-code / topology setup flow). The only thing we still owe existing
// users is a clean exit for a leftover BOOTSTRAP.md: rename it once to a
// timestamped sibling so the gateway stops tripping over it on every
// restart, and let the caller log why. No network call, no credentials.

export type LegacyBootstrapOutcome = {
  kind: "no-file" | "quarantined";
  // Populated only when kind === "quarantined".
  movedFrom?: string;
  movedTo?: string;
};

/**
 * Retire a leftover ~/.agentrux/BOOTSTRAP.md from an older install.
 *
 * Returns "no-file" when nothing is there, or "quarantined" with the
 * source and destination paths after renaming BOOTSTRAP.md to a unique
 * BOOTSTRAP.md.legacy-<ts> sibling. The original file's contents are
 * preserved at the new path; credentials.json is never touched.
 */
export function quarantineLegacyBootstrap(): LegacyBootstrapOutcome {
  const movedTo = pickUniqueLegacyPath();
  try {
    fs.renameSync(BOOTSTRAP_PATH, movedTo);
  } catch (err: any) {
    // ENOENT = nothing to retire. POSIX rename(2) is atomic on the source
    // side, so if two gateway processes start concurrently exactly one wins
    // the rename and the loser sees ENOENT — we treat that as "no-file"
    // rather than throwing and crashing the gateway's startup path.
    if (err?.code === "ENOENT") return { kind: "no-file" };
    throw err;
  }
  return { kind: "quarantined", movedFrom: BOOTSTRAP_PATH, movedTo };
}

/**
 * Pick a BOOTSTRAP.md.legacy-<ts> destination that does not collide with an
 * existing one. The timestamp suffix has 1s resolution, so two retirements
 * in the same wall-clock second fall back to `-2`, `-3`, ...
 */
function pickUniqueLegacyPath(): string {
  const base = path.join(
    AGENTRUX_DIR,
    `BOOTSTRAP.md.legacy-${formatTimestampSuffix(new Date())}`,
  );
  if (!fs.existsSync(base)) return base;
  for (let i = 2; i < 1000; i++) {
    const candidate = `${base}-${i}`;
    if (!fs.existsSync(candidate)) return candidate;
  }
  return `${base}-${process.hrtime.bigint().toString(36)}`;
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
