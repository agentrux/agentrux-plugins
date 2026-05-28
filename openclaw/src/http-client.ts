/**
 * AgenTrux HTTP client — no external dependencies.
 *
 * Token lifecycle (OAuth 2.1 client_credentials, Phase 1.9+):
 *   - POST /oauth/token (form-encoded) issues a Bearer access_token
 *     (`aat_<JWT>`) with `expires_in` seconds, no refresh_token.
 *   - On expiry / 401 we just re-issue. `client_credentials` does not
 *     have a refresh leg, so all the old /auth/refresh plumbing is gone.
 */

import * as https from "https";
import * as http from "http";
import { type Credentials } from "./credentials";

// AgenTrux prod ALB rejects requests without a User-Agent header (403 from
// awselb/2.0), and Node's native http(s).request does not set one by default.
// Surface a stable identifier so server-side telemetry can attribute traffic
// and the WAF allows the request through. Major version is enough for the
// WAF; release tooling can bump as needed.
export const PLUGIN_USER_AGENT = "agentrux-openclaw-plugin/1.x (+node)";

// ---------------------------------------------------------------------------
// ID prefix normalization
// ---------------------------------------------------------------------------

/**
 * Prepend the `top_` prefix to a topic ID if it isn't already present.
 *
 * `pipe_router` enforces `top_<uuid>` on all data-plane paths (publish, list,
 * stream, payloads). Plugin callers may have a bare UUID in hand (e.g. from
 * a JWT scope claim or an old config file), so we normalize at the boundary.
 */
export function ensureTopPrefix(topicId: string): string {
  return topicId.startsWith("top_") ? topicId : `top_${topicId}`;
}

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------

export function httpJson(
  method: string,
  url: string,
  body?: Record<string, unknown>,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const opts = {
      method,
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      headers: {
        "Content-Type": "application/json",
        "User-Agent": PLUGIN_USER_AGENT,
        ...headers,
      },
    };
    const req = mod.request(opts, (res) => {
      let raw = "";
      res.on("data", (c: Buffer) => (raw += c.toString()));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode || 0, data: JSON.parse(raw) });
        } catch {
          resolve({ status: res.statusCode || 0, data: raw });
        }
      });
    });
    req.on("error", reject);
    if (body) req.write(JSON.stringify(body));
    req.end();
  });
}

/**
 * Form-encoded POST. OAuth 2.1 §3.2 requires /token to accept
 * application/x-www-form-urlencoded; AgenTrux follows the spec.
 */
export function httpForm(
  url: string,
  form: Record<string, string>,
): Promise<{ status: number; data: any }> {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const body = new URLSearchParams(form).toString();
    const opts = {
      method: "POST",
      hostname: u.hostname,
      port: u.port,
      path: u.pathname + u.search,
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": Buffer.byteLength(body).toString(),
        "User-Agent": PLUGIN_USER_AGENT,
      },
    };
    const req = mod.request(opts, (res) => {
      let raw = "";
      res.on("data", (c: Buffer) => (raw += c.toString()));
      res.on("end", () => {
        try {
          resolve({ status: res.statusCode || 0, data: JSON.parse(raw) });
        } catch {
          resolve({ status: res.statusCode || 0, data: raw });
        }
      });
    });
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Token manager with auto-refresh
// ---------------------------------------------------------------------------

interface TokenState {
  access_token: string;   // "aat_<JWT>"
  expires_at: number;     // epoch ms, derived from server's `expires_in`
}

let tokenState: TokenState | null = null;

// Single-flight gate. Concurrent callers that arrive while a /oauth/token
// issue is in progress await the same in-flight promise instead of each
// issuing their own request. Even without refresh-token semantics, this
// matters for the rate limit on /oauth/token (server-side throttle) and
// avoids unnecessary CPU on the auth path during burst traffic.
let inflight: Promise<string> | null = null;

export async function ensureToken(creds: Credentials): Promise<string> {
  // Fast path: cached access_token still valid (60s safety margin).
  if (tokenState && tokenState.expires_at > Date.now() + 60_000) {
    return tokenState.access_token;
  }

  // Coalesce concurrent callers onto a single in-flight token issue.
  if (inflight) return inflight;

  inflight = (async () => {
    try {
      // OAuth 2.1 client_credentials grant. Form-encoded body (RFC 6749 §4.4).
      const r = await httpForm(`${creds.base_url}/oauth/token`, {
        grant_type: "client_credentials",
        client_id: creds.client_id,
        client_secret: creds.client_secret,
      });
      if (r.status !== 200 || !r.data?.access_token) {
        throw new Error(`Auth failed (${r.status}): ${JSON.stringify(r.data)}`);
      }
      const expiresInSeconds = typeof r.data.expires_in === "number"
        ? r.data.expires_in
        : 600; // server default; safe lower bound
      tokenState = {
        access_token: r.data.access_token,
        expires_at: Date.now() + expiresInSeconds * 1000,
      };
      return tokenState.access_token;
    } finally {
      inflight = null;
    }
  })();

  return inflight;
}

export function invalidateToken(): void {
  tokenState = null;
}

/**
 * Compare-and-clear: only clear the cached token state if its current
 * access_token matches `expected`. Used by authRequest() so that a stale
 * 401 from a request that went out before another caller refreshed does
 * NOT clobber the fresh token. Without this guard, a burst of expiring
 * requests can each trigger a redundant /auth/token after a sibling has
 * already refreshed.
 *
 * Returns true if the token was cleared.
 */
export function invalidateIfStillCurrent(expected: string): boolean {
  if (tokenState && tokenState.access_token === expected) {
    tokenState = null;
    return true;
  }
  return false;
}

// ---------------------------------------------------------------------------
// Authenticated requests
// ---------------------------------------------------------------------------

export async function authRequest(
  creds: Credentials,
  method: string,
  urlPath: string,
  body?: Record<string, unknown>,
): Promise<any> {
  const token = await ensureToken(creds);
  const r = await httpJson(method, `${creds.base_url}${urlPath}`, body, {
    Authorization: `Bearer ${token}`,
  });
  if (r.status === 401) {
    // Compare-and-clear: only invalidate if the cached token is still the
    // one we just used. If a sibling caller has already refreshed in the
    // meantime, the cached token is fresh and the 401 is stale — clearing
    // it would force an unnecessary /auth/token round trip.
    invalidateIfStillCurrent(token);
    const newToken = await ensureToken(creds);
    const retry = await httpJson(method, `${creds.base_url}${urlPath}`, body, {
      Authorization: `Bearer ${newToken}`,
    });
    if (retry.status >= 400) throw new Error(`Request failed: ${JSON.stringify(retry.data)}`);
    return retry.data;
  }
  if (r.status >= 400) throw new Error(`Request failed (${r.status}): ${JSON.stringify(r.data)}`);
  return r.data;
}

// ---------------------------------------------------------------------------
// AgenTrux API operations
// ---------------------------------------------------------------------------

/**
 * Pull events from a topic (Phase 2.5a SSOT)。
 * Cursor は evt_id 文字列 (旧 ?after_sequence_no は廃止)。
 * Response shape: {events: [...], next: {...}} (旧 items は廃止)。
 */
export async function pullEvents(
  creds: Credentials,
  topicId: string,
  afterEventId: string | null,
  limit = 20,
  order: "asc" | "desc" = "asc",
  opts: { excludeSelf?: boolean } = {},
): Promise<any[]> {
  const qs = new URLSearchParams();
  qs.set("limit", String(limit));
  qs.set("order", order);
  if (afterEventId) qs.set("after", afterEventId);
  // Server-side echo filter (see echo_policy.md §2-1). Set when the caller
  // also publishes to this topic, to avoid feeding own writes back into
  // the agent loop.
  if (opts.excludeSelf) qs.set("exclude_self", "true");
  const result = await authRequest(
    creds,
    "GET",
    `/topics/${ensureTopPrefix(topicId)}/events?${qs.toString()}`,
  );
  return result.events || [];
}

/**
 * Publish an event (Phase 2.2 SSOT)。
 * Body shape: {event_type, payload, metadata?, payload_object_id?}。
 * 旧 root field (type / correlation_id / reply_topic) は廃止、 metadata 内に折りたたむ。
 */
export async function publishEvent(
  creds: Credentials,
  topicId: string,
  eventType: string,
  payload: Record<string, unknown>,
  opts?: { metadata?: Record<string, unknown>; payload_object_id?: string },
): Promise<string> {
  const body: Record<string, unknown> = { event_type: eventType, payload };
  if (opts?.metadata) body.metadata = opts.metadata;
  if (opts?.payload_object_id) body.payload_object_id = opts.payload_object_id;
  const result = await authRequest(creds, "POST", `/topics/${ensureTopPrefix(topicId)}/events`, body);
  return result.event_id;
}

/**
 * Upload a file to AgenTrux via presigned URL (Phase 2.4a SSOT)。
 * Returns { payload_object_id, presigned_get_url } for attaching to events。
 *
 * 新 spec:
 *   - request body: {size_bytes, content_type?, checksum_sha256?}
 *   - response: {payload_object_id, presigned_put_url, required_headers, ...}
 *   - PUT は required_headers を必須で送る (S3 署名検証)
 *   - download URL は別 GET /topics/{top_id}/payloads/{pob_id} で取得 (response.presigned_get_url)
 */
export async function uploadFile(
  creds: Credentials,
  topicId: string,
  filePath: string,
  contentType: string,
): Promise<{ payload_object_id: string; presigned_get_url: string }> {
  const fs = await import("fs");
  const crypto = await import("crypto");
  const data = fs.readFileSync(filePath);
  // S3 requires `x-amz-checksum-sha256` in base64 form; AgenTrux server
  // accepts base64 (44) or hex (64) here but uses the same value verbatim
  // in `required_headers`. Send base64 to match the S3 PUT signature.
  const checksum_sha256 = crypto.createHash("sha256").update(data).digest("base64");

  const topId = ensureTopPrefix(topicId);
  // 1. Get presigned PUT URL
  const info = await authRequest(creds, "POST", `/topics/${topId}/payloads`, {
    size_bytes: data.length,
    content_type: contentType,
    checksum_sha256,
  });

  // 2. PUT file to presigned URL with required_headers. Use native fetch
  // so the URL is sent verbatim — http.request decomposition perturbed the
  // `host` header and broke the SigV4 signature on SSE-KMS buckets.
  const putRes = await fetch(info.presigned_put_url, {
    method: "PUT",
    headers: info.required_headers || {},
    body: new Uint8Array(data),
  });
  if (!putRes.ok) {
    const body = (await putRes.text()).slice(0, 800);
    throw new Error(`Upload failed: ${putRes.status} ${body}`);
  }

  // Note: GET /topics/{id}/payloads/{pob_id} はここでは呼ばない。 server 仕様で
  // pob 状態が pending → committed に遷移するのは publish_event (object_ref)
  // 経由の HEAD verify 後。 PUT 直後は pending のため GET は 404 になる。
  // caller (publishOutboundPayload / agentrux_deliver) が後で publish_event を
  // 呼んだ時点で commit され、 必要なら別 endpoint で download URL を取れる。
  return {
    payload_object_id: info.payload_object_id,
    presigned_get_url: "",
  };
}
