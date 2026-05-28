// AgenTrux Topology Request Flow v1 install helper (TypeScript / Node.js).
//
// SSOT: docs/04_design/auth/topology_request_v1.md
//
// Public API:
//   import {
//     installTopology,
//     type TopologyDeclaration,
//     type TopologyTopicSpec,
//     type TopologyGrantSpec,
//     type InstallResult,
//     type InstallPendingInfo,
//     InstallError, InstallDeniedError, InstallTimeoutError,
//   } from "./topology-install";
//
//   const result = await installTopology({
//     baseUrl: "https://api.agentrux.com",
//     clientId: "<oauth-public-client-id>",
//     declaration: { ... },
//     onUserCode: (info) => console.log(`Visit ${info.verificationUriComplete}`),
//   });
//
// 設計判断:
//   - fetch ベース (Node 22+ で native、 cross-runtime 互換)
//   - User-Agent 明示 (AWS ALB が UA 欠落を 403 ブロックする問題、 既存 plugin 修正済)
//   - 仕様違反は早期に client side で reject (UX 親切化)
//   - polling は setTimeout-based、 AbortSignal で中断可

// Step 1a (device_code_setup_v1.md §5-1): polling / error parsing / Install* 階層を
// 共有 module に抽出。 public API は本 module 経由 re-export で不変保持。
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

// Re-export for backward compatibility (既存 caller の `from "./topology-install"` を維持)
export {
  InstallAbortedError,
  InstallAuthError,
  InstallConfigError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
};

const TOPOLOGY_REQUEST_PATH = "/oauth/topology-request";
const USER_AGENT = "agentrux-sdk-topology-install-ts/1.0";
const DEFAULT_TIMEOUT_S = 600;
const MAX_TIMEOUT_S = 600; // device_code TTL (RFC 8628)

// ----------------------------------------------------------------------------
// Types
// ----------------------------------------------------------------------------

export type GrantScope = "read" | "write";

export interface TopologyTopicSpec {
  ref: string; // agent ↔ picker ↔ token 間の連結 key
  name: string; // ^[a-z0-9._-]{1,128}$
  retention_s: number; // [3600, 2592000]
  intent?: string | null; // picker 表示用
}

export interface TopologyGrantSpec {
  topic_ref: string;
  scope: GrantScope;
  binding_name?: string | null; // 1 binding_name = 1 grant
}

export interface TopologyDeclaration {
  script_name: string;
  description: string;
  topics: TopologyTopicSpec[];
  grants: TopologyGrantSpec[];
  policy_match_inputs?: Record<string, unknown> | null;
  version?: number; // default 1
}

export interface InstallPendingInfo {
  userCode: string;
  verificationUri: string;
  verificationUriComplete: string;
  expiresIn: number;
  interval: number;
}

export interface InstallResultGrant {
  topicScopeKey: string; // "topic:top_<uuid>:<scope>"
  grantId: string;
  bindingName: string | null;
}

export interface InstallResult {
  accessToken: string; // aat_<JWT>
  refreshToken: string; // art_<opaque>
  expiresIn: number;
  scope: string[];
  scriptId: string;
  aliasId: string;
  topicIdMap: Record<string, string>; // ref → top_<uuid>
  grants: InstallResultGrant[];
  grantedAtMs: number;
}

export type OnUserCode = (info: InstallPendingInfo) => void | Promise<void>;

export interface InstallTopologyOptions {
  baseUrl: string;
  clientId: string;
  declaration: TopologyDeclaration;
  onUserCode: OnUserCode;
  clientHint?: string;
  timeoutSeconds?: number;
  signal?: AbortSignal;
  /** custom fetch for testing. defaults to globalThis.fetch */
  fetchImpl?: typeof fetch;
}

// ----------------------------------------------------------------------------
// Errors — Step 1a で oauth-errors.ts に集約済、 ここでは re-export のみ (上部)
// ----------------------------------------------------------------------------


// ----------------------------------------------------------------------------
// Client-side declaration validation (early fail for better UX)
// ----------------------------------------------------------------------------

// Codex MF-1 補完: server 側 _strip_validate_string / RAR size limit と整合する
// client-side checks。 早期 reject で UX 親切化 + payload を small に保つ。
const VALID_SCOPES = new Set<GrantScope>(["read", "write"]);
const MAX_TOPICS = 20;
const MAX_GRANTS = 40;
const MAX_DESCRIPTION = 256;
const MAX_CLIENT_HINT = 256;
const MAX_INTENT = 256;
const MAX_RAR_BYTES = 16 * 1024;
const BINDING_NAME_MIN = 1;
const BINDING_NAME_MAX = 64;
// DDL CHECK と一致: ^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$
const BINDING_NAME_RE = /^[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?$/;

function rejectControlChars(value: string, field: string): void {
  for (let i = 0; i < value.length; i++) {
    const c = value.charCodeAt(i);
    if (c < 0x20 || c === 0x7f) {
      throw new InstallConfigError(`${field} contains control character`);
    }
  }
}

export function validateDeclaration(d: TopologyDeclaration): void {
  if ((d.version ?? 1) !== 1) {
    throw new InstallConfigError(`unsupported topology version: ${d.version}`);
  }
  if (!d.script_name) throw new InstallConfigError("script_name must be non-empty");
  if (!d.description) throw new InstallConfigError("description must be non-empty");
  if (d.description.length > MAX_DESCRIPTION) {
    throw new InstallConfigError(
      `description exceeds ${MAX_DESCRIPTION} chars`,
    );
  }
  rejectControlChars(d.script_name, "script_name");
  rejectControlChars(d.description, "description");
  if (!Array.isArray(d.topics) || d.topics.length === 0) {
    throw new InstallConfigError("at least 1 topic required");
  }
  if (d.topics.length > MAX_TOPICS) {
    throw new InstallConfigError(`topics exceeds limit ${MAX_TOPICS}`);
  }
  if (!Array.isArray(d.grants) || d.grants.length === 0) {
    throw new InstallConfigError("at least 1 grant required");
  }
  if (d.grants.length > MAX_GRANTS) {
    throw new InstallConfigError(`grants exceeds limit ${MAX_GRANTS}`);
  }
  const topicRefs = new Set(d.topics.map((t) => t.ref));
  for (let i = 0; i < d.topics.length; i++) {
    const t = d.topics[i];
    if (t.intent != null && t.intent.length > MAX_INTENT) {
      throw new InstallConfigError(
        `topics[${i}].intent exceeds ${MAX_INTENT} chars`,
      );
    }
    if (t.intent != null) rejectControlChars(t.intent, `topics[${i}].intent`);
    rejectControlChars(t.ref, `topics[${i}].ref`);
    rejectControlChars(t.name, `topics[${i}].name`);
  }
  const seenBindings = new Set<string>();
  const seenTopicScope = new Set<string>();
  for (let i = 0; i < d.grants.length; i++) {
    const g = d.grants[i];
    if (!VALID_SCOPES.has(g.scope)) {
      throw new InstallConfigError(
        `grants[${i}].scope=${JSON.stringify(g.scope)} must be 'read' or 'write'`,
      );
    }
    if (!topicRefs.has(g.topic_ref)) {
      throw new InstallConfigError(
        `grants[${i}].topic_ref=${JSON.stringify(g.topic_ref)} not in topics`,
      );
    }
    const key = `${g.topic_ref}:${g.scope}`;
    if (seenTopicScope.has(key)) {
      throw new InstallConfigError(
        `grants[${i}] duplicate (topic_ref, scope) entry`,
      );
    }
    seenTopicScope.add(key);
    if (g.binding_name != null) {
      if (g.binding_name.length < BINDING_NAME_MIN || g.binding_name.length > BINDING_NAME_MAX) {
        throw new InstallConfigError(
          `grants[${i}].binding_name length must be in [${BINDING_NAME_MIN}, ${BINDING_NAME_MAX}]`,
        );
      }
      if (!BINDING_NAME_RE.test(g.binding_name)) {
        throw new InstallConfigError(
          `grants[${i}].binding_name=${JSON.stringify(g.binding_name)} ` +
            "must match ^[\\x21-\\x7e]([\\x20-\\x7e]*[\\x21-\\x7e])?$",
        );
      }
      if (seenBindings.has(g.binding_name)) {
        throw new InstallConfigError(
          `grants[${i}].binding_name=${JSON.stringify(g.binding_name)} duplicate`,
        );
      }
      seenBindings.add(g.binding_name);
    }
  }
  // full RAR payload 16KB check
  const rarJson = buildAuthorizationDetails(d);
  // utf-8 byte length: Buffer は Node 環境、 TextEncoder は cross-runtime
  const byteLen =
    typeof Buffer !== "undefined"
      ? Buffer.byteLength(rarJson, "utf-8")
      : new TextEncoder().encode(rarJson).byteLength;
  if (byteLen > MAX_RAR_BYTES) {
    throw new InstallConfigError(
      `authorization_details exceeds ${MAX_RAR_BYTES} bytes`,
    );
  }
}

export function buildAuthorizationDetails(d: TopologyDeclaration): string {
  return JSON.stringify([
    {
      type: "agentrux.topology",
      version: d.version ?? 1,
      script: {
        name: d.script_name,
        description: d.description,
      },
      topics: d.topics.map((t) => ({
        ref: t.ref,
        name: t.name,
        retention_s: t.retention_s,
        intent: t.intent ?? null,
      })),
      grants: d.grants.map((g) => ({
        topic_ref: g.topic_ref,
        scope: g.scope,
        binding_name: g.binding_name ?? null,
      })),
      policy_match_inputs: d.policy_match_inputs ?? null,
    },
  ]);
}

// ----------------------------------------------------------------------------
// Main entry point
// ----------------------------------------------------------------------------

export async function installTopology(
  opts: InstallTopologyOptions,
): Promise<InstallResult> {
  if (!opts.baseUrl || !/^https?:\/\//.test(opts.baseUrl)) {
    throw new InstallConfigError(`baseUrl must be http(s): ${opts.baseUrl}`);
  }
  if (!opts.clientId) {
    throw new InstallConfigError("clientId is required");
  }
  if (opts.clientHint != null) {
    if (opts.clientHint.length > MAX_CLIENT_HINT) {
      throw new InstallConfigError(
        `clientHint exceeds ${MAX_CLIENT_HINT} chars`,
      );
    }
    rejectControlChars(opts.clientHint, "clientHint");
  }
  validateDeclaration(opts.declaration);

  const base = opts.baseUrl.replace(/\/+$/, "");
  const fetchImpl: typeof fetch =
    opts.fetchImpl ??
    (globalThis.fetch as typeof fetch | undefined) ??
    (() => {
      throw new InstallConfigError(
        "fetch is not available in this runtime (provide opts.fetchImpl)",
      );
    });
  const timeoutS = Math.max(60, Math.min(opts.timeoutSeconds ?? DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S));

  // 1. issue topology-request
  const { deviceCode, pending } = await issueTopologyRequest({
    base,
    clientId: opts.clientId,
    declaration: opts.declaration,
    clientHint: opts.clientHint,
    fetchImpl,
    signal: opts.signal,
  });

  // 2. notify operator
  const cbRet = opts.onUserCode(pending);
  if (cbRet && typeof (cbRet as Promise<void>).then === "function") {
    await cbRet;
  }

  // 3. poll /oauth/token
  return await pollToken({
    base,
    clientId: opts.clientId,
    deviceCode,
    pending,
    timeoutS,
    fetchImpl,
    signal: opts.signal,
  });
}

// ----------------------------------------------------------------------------
// Internals
// ----------------------------------------------------------------------------

interface IssueArgs {
  base: string;
  clientId: string;
  declaration: TopologyDeclaration;
  clientHint?: string;
  fetchImpl: typeof fetch;
  signal?: AbortSignal;
}

async function issueTopologyRequest(
  args: IssueArgs,
): Promise<{ deviceCode: string; pending: InstallPendingInfo }> {
  const form = new URLSearchParams();
  form.set("client_id", args.clientId);
  form.set("authorization_details", buildAuthorizationDetails(args.declaration));
  if (args.clientHint) form.set("client_hint", args.clientHint);

  const res = await safeFetch(
    args.fetchImpl,
    `${args.base}${TOPOLOGY_REQUEST_PATH}`,
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
  if (res.status === 400 && code.startsWith("unsupported_authorization_details")) {
    throw new InstallError(`${code}: ${desc}`, code, res.status);
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

interface PollArgs {
  base: string;
  clientId: string;
  deviceCode: string;
  pending: InstallPendingInfo;
  timeoutS: number;
  fetchImpl: typeof fetch;
  signal?: AbortSignal;
}

async function pollToken(args: PollArgs): Promise<InstallResult> {
  // Step 1a: polling loop は oauth-polling.pollDeviceToken() に抽出済 (挙動不変)。
  // 本 wrapper は topology 固有の result parse のみ実施。
  const body = await pollDeviceToken({
    base: args.base,
    clientId: args.clientId,
    deviceCode: args.deviceCode,
    userCode: args.pending.userCode,
    timeoutS: args.timeoutS,
    initialIntervalSeconds: args.pending.interval,
    fetchImpl: args.fetchImpl,
    signal: args.signal,
  });
  return parseTokenResponse(body);
}

function parseTokenResponse(body: Record<string, unknown>): InstallResult {
  // Codex MF-2: token response shape を strict に検証。 "undefined" や silent skip を避ける。
  if (typeof body.access_token !== "string" || !body.access_token) {
    throw new InstallError("malformed token response: access_token missing");
  }
  if (typeof body.refresh_token !== "string" || !body.refresh_token) {
    throw new InstallError("malformed token response: refresh_token missing");
  }
  const expiresIn = Number(body.expires_in);
  if (!Number.isFinite(expiresIn)) {
    throw new InstallError("malformed token response: expires_in invalid");
  }
  const accessToken = body.access_token;
  const refreshToken = body.refresh_token;
  const scope = String(body.scope ?? "").split(/\s+/).filter(Boolean);

  const ad = body.authorization_details;
  if (!Array.isArray(ad) || ad.length === 0) {
    throw new InstallError(
      "token response missing authorization_details (was this a topology request?)",
    );
  }
  const entry = ad[0] as Record<string, unknown>;
  const granted = entry.granted as Record<string, unknown> | undefined;
  if (!granted || typeof granted !== "object") {
    throw new InstallError("authorization_details[0].granted missing");
  }
  if (typeof granted.script_id !== "string" || !granted.script_id) {
    throw new InstallError("granted.script_id must be non-empty string");
  }
  if (typeof granted.alias_id !== "string" || !granted.alias_id) {
    throw new InstallError("granted.alias_id must be non-empty string");
  }
  const topicIdMapRaw = granted.topic_id_map;
  if (
    topicIdMapRaw != null &&
    (typeof topicIdMapRaw !== "object" || Array.isArray(topicIdMapRaw))
  ) {
    throw new InstallError("granted.topic_id_map must be object");
  }
  const topicIdMap: Record<string, string> = {};
  for (const [k, v] of Object.entries(
    (topicIdMapRaw as Record<string, unknown>) ?? {},
  )) {
    if (typeof v !== "string" || !v) {
      throw new InstallError(
        `topic_id_map[${JSON.stringify(k)}] must be non-empty string`,
      );
    }
    topicIdMap[k] = v;
  }
  const grantIdsRaw = granted.grant_ids;
  if (
    grantIdsRaw != null &&
    (typeof grantIdsRaw !== "object" || Array.isArray(grantIdsRaw))
  ) {
    throw new InstallError("granted.grant_ids must be object");
  }
  const grants: InstallResultGrant[] = [];
  for (const [key, val] of Object.entries(
    (grantIdsRaw as Record<string, unknown>) ?? {},
  )) {
    if (!val || typeof val !== "object" || Array.isArray(val)) {
      throw new InstallError(`grant_ids[${JSON.stringify(key)}] must be object`);
    }
    const v = val as Record<string, unknown>;
    if (typeof v.grant_id !== "string" || !v.grant_id) {
      throw new InstallError(
        `grant_ids[${JSON.stringify(key)}].grant_id must be non-empty string`,
      );
    }
    if (v.binding_name != null && typeof v.binding_name !== "string") {
      throw new InstallError(
        `grant_ids[${JSON.stringify(key)}].binding_name must be string or null`,
      );
    }
    grants.push({
      topicScopeKey: key,
      grantId: v.grant_id,
      bindingName: v.binding_name == null ? null : (v.binding_name as string),
    });
  }
  return {
    accessToken,
    refreshToken,
    expiresIn,
    scope,
    scriptId: granted.script_id,
    aliasId: granted.alias_id,
    topicIdMap,
    grants,
    grantedAtMs: Date.now(),
  };
}

// parseOAuthError / safeFetch / delay は Step 1a で ./oauth-errors と ./oauth-polling に
// 抽出済。 本 module は import 経由で利用 (issueTopologyRequest 内 safeFetch / parseOAuthError、
// pollToken 内は pollDeviceToken に委譲)。
