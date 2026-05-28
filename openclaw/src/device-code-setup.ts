// Plain Device Code (RFC 8628) plugin setup helper — no RAR.
//
// SSOT: docs/04_design/auth/device_code_setup_v1.md §3-2
//
// installTopology() (RAR 拡張版) と並列に、 RAR なしの単純な device code 経由 credential
// 取得を TS SDK で提供する。 backend は既存 RFC 8628 endpoint をそのまま reuse。
//
// Public API:
//   import {
//     setupViaDeviceCode,
//     type DeviceCodeSetupResult,
//     type DeviceCodeSetupPending,
//     InstallError, InstallDeniedError, InstallTimeoutError, InstallAuthError,
//     InstallAbortedError, InstallConfigError,
//   } from "./device-code-setup";

import {
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
  parseOAuthError,
} from "./oauth-errors";
import { pollDeviceToken, safeFetch } from "./oauth-polling";

// Re-export error hierarchy for caller convenience (single import surface).
export {
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
};

// ---------------------------------------------------------------------------
// Device credentials persistence (inline、 host file ~/.agentrux/device_credentials.json)
//
// 注: 既存 credentials.ts は plugins/ 配下で gitignore 対象 (.gitignore の *credentials*
// pattern)。 そのため DeviceCredentials の save/load helper は本 module に inline する。
// SSOT: docs/04_design/auth/device_code_setup_v1.md §4-1
// ---------------------------------------------------------------------------

import * as fs from "node:fs";
import * as path from "node:path";

export interface DeviceCredentials {
  base_url: string;
  dcr_client_id: string;
  access_token: string;
  refresh_token: string | null;
  issued_at_unix: number;
  expires_in: number;
  scope: string[];
  id_token?: string;
}

function agentruxDir(): string {
  const home = process.env.HOME || process.env.USERPROFILE || ".";
  return path.join(home, ".agentrux");
}

function deviceCredentialsPath(): string {
  return path.join(agentruxDir(), "device_credentials.json");
}

export function saveDeviceCredentials(creds: DeviceCredentials): void {
  const dir = agentruxDir();
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  }
  fs.writeFileSync(deviceCredentialsPath(), JSON.stringify(creds, null, 2), {
    mode: 0o600,
  });
}

export function loadDeviceCredentials(): DeviceCredentials | null {
  const filePath = deviceCredentialsPath();
  try {
    if (!fs.existsSync(filePath)) return null;
    const raw = JSON.parse(fs.readFileSync(filePath, "utf-8"));
    if (!raw || typeof raw !== "object") return null;
    if (
      typeof raw.dcr_client_id !== "string" ||
      typeof raw.access_token !== "string"
    ) {
      return null;
    }
    return raw as DeviceCredentials;
  } catch {
    return null;
  }
}

// Scope vocabulary (server is_valid_authorize_scope() と整合)
const VALID_SCOPE_VOCAB: ReadonlySet<string> = new Set([
  "topic.read",
  "topic.write",
  "openid",
  "email",
  "profile",
]);

const DEVICE_AUTHORIZE_PATH = "/oauth/device/authorize";
const USER_AGENT = "agentrux-sdk-device-code-setup-ts/1.0";
const DEFAULT_TIMEOUT_S = 600;
const MIN_TIMEOUT_S = 60;
const MAX_TIMEOUT_S = 600;

export interface DeviceCodeSetupPending {
  userCode: string;
  verificationUri: string;
  verificationUriComplete: string;
  expiresIn: number;
  interval: number;
}

export interface DeviceCodeSetupResult {
  accessToken: string;
  refreshToken: string | null;
  scope: string[];
  expiresIn: number;
  idToken?: string;
  grantedScopes: string[]; // alias of `scope` (legacy compat)
  grantedAtMs: number;
}

export type OnUserCode = (info: DeviceCodeSetupPending) => void | Promise<void>;

export interface SetupViaDeviceCodeOptions {
  baseUrl: string;
  clientId: string;
  scope?: readonly string[]; // default ["topic.read", "topic.write"]
  onUserCode?: OnUserCode;
  timeoutSeconds?: number; // default 600, clamped to [60, 600]
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

function rejectControlChars(value: string, field: string): void {
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) {
      throw new InstallConfigError(`${field} contains control character`);
    }
  }
}

function validateScope(scope: readonly string[]): void {
  if (scope.length === 0) {
    throw new InstallConfigError("scope must be non-empty");
  }
  const seen = new Set<string>();
  for (const s of scope) {
    if (typeof s !== "string" || s.length === 0) {
      throw new InstallConfigError(
        `scope entry must be non-empty string: ${JSON.stringify(s)}`,
      );
    }
    if (seen.has(s)) {
      throw new InstallConfigError(`scope duplicate: ${JSON.stringify(s)}`);
    }
    seen.add(s);
    if (!VALID_SCOPE_VOCAB.has(s)) {
      throw new InstallConfigError(
        `scope ${JSON.stringify(s)} not in vocabulary ` +
          `(${JSON.stringify([...VALID_SCOPE_VOCAB].sort())})`,
      );
    }
    rejectControlChars(s, `scope[${JSON.stringify(s)}]`);
  }
}

export async function setupViaDeviceCode(
  opts: SetupViaDeviceCodeOptions,
): Promise<DeviceCodeSetupResult> {
  // 1. validate input
  if (
    typeof opts.baseUrl !== "string" ||
    !/^https?:\/\//.test(opts.baseUrl)
  ) {
    throw new InstallConfigError(`baseUrl must be http(s): ${opts.baseUrl}`);
  }
  if (typeof opts.clientId !== "string" || !opts.clientId) {
    throw new InstallConfigError("clientId is required");
  }
  rejectControlChars(opts.clientId, "clientId");
  const scope = opts.scope ?? ["topic.read", "topic.write"];
  validateScope(scope);

  const base = opts.baseUrl.replace(/\/+$/, "");
  const fetchImpl: typeof fetch =
    opts.fetchImpl ??
    (globalThis.fetch as typeof fetch | undefined) ??
    (() => {
      throw new InstallConfigError(
        "fetch is not available in this runtime (provide opts.fetchImpl)",
      );
    });
  const timeoutS = Math.max(
    MIN_TIMEOUT_S,
    Math.min(opts.timeoutSeconds ?? DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S),
  );

  // 2. issue device_code
  const { deviceCode, pending } = await issueDeviceCode({
    base,
    clientId: opts.clientId,
    scope,
    fetchImpl,
    signal: opts.signal,
  });

  // 3. notify operator
  if (opts.onUserCode) {
    const cbRet = opts.onUserCode(pending);
    if (cbRet && typeof (cbRet as Promise<void>).then === "function") {
      await cbRet;
    }
  }

  // 4. poll /oauth/token (RFC 8628 §3.4、 oauth-polling 経由)
  const body = await pollDeviceToken({
    base,
    clientId: opts.clientId,
    deviceCode,
    userCode: pending.userCode,
    timeoutS,
    initialIntervalSeconds: pending.interval,
    fetchImpl,
    signal: opts.signal,
  });
  return parseTokenResponse(body);
}

interface IssueArgs {
  base: string;
  clientId: string;
  scope: readonly string[];
  fetchImpl: typeof fetch;
  signal?: AbortSignal;
}

async function issueDeviceCode(
  args: IssueArgs,
): Promise<{ deviceCode: string; pending: DeviceCodeSetupPending }> {
  const form = new URLSearchParams();
  form.set("client_id", args.clientId);
  form.set("scope", args.scope.join(" "));

  const res = await safeFetch(
    args.fetchImpl,
    `${args.base}${DEVICE_AUTHORIZE_PATH}`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
        Accept: "application/json",
      },
      body: form.toString(),
      signal: args.signal,
    },
  );

  if (res.status === 200) {
    const body = (await res.json()) as Record<string, unknown>;
    return {
      deviceCode: String(body.device_code),
      pending: {
        userCode: String(body.user_code),
        verificationUri: String(body.verification_uri),
        verificationUriComplete: String(body.verification_uri_complete),
        expiresIn: Number(body.expires_in),
        interval: Number(body.interval),
      },
    };
  }

  const { code, desc } = await parseOAuthError(res);
  if (res.status === 400 && code === "invalid_client") {
    throw new InstallAuthError(`invalid_client: ${desc}`);
  }
  if (res.status === 400 && code === "invalid_scope") {
    throw new InstallAuthError(`invalid_scope: ${desc}`);
  }
  if (res.status === 400) {
    throw new InstallError(
      `${code || "invalid_request"}: ${desc}`,
      code || "invalid_request",
      400,
    );
  }
  if (res.status === 429) {
    throw new InstallError(`rate_limited: ${desc}`, "rate_limited", 429);
  }
  throw new InstallError(
    `unexpected status ${res.status}: ${desc}`,
    code,
    res.status,
  );
}

function parseTokenResponse(body: Record<string, unknown>): DeviceCodeSetupResult {
  if (typeof body.access_token !== "string" || !body.access_token) {
    throw new InstallError("malformed token response: access_token missing");
  }
  const expiresIn = Number(body.expires_in);
  if (!Number.isFinite(expiresIn)) {
    throw new InstallError("malformed token response: expires_in invalid");
  }
  const refreshTokenRaw = body.refresh_token;
  const refreshToken =
    typeof refreshTokenRaw === "string" && refreshTokenRaw
      ? refreshTokenRaw
      : null;
  const scopeRaw = body.scope;
  const scope =
    typeof scopeRaw === "string"
      ? scopeRaw.split(/\s+/).filter(Boolean)
      : [];
  const idTokenRaw = body.id_token;
  const idToken =
    typeof idTokenRaw === "string" && idTokenRaw ? idTokenRaw : undefined;

  return {
    accessToken: body.access_token,
    refreshToken,
    scope,
    expiresIn,
    idToken,
    grantedScopes: scope,
    grantedAtMs: Date.now(),
  };
}
