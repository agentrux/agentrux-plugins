/**
 * AgenTrux HTTP client — no external dependencies.
 * Handles JWT auth with auto-refresh.
 */

import * as https from "https";
import * as http from "http";
import { type Credentials } from "./credentials";

// ---------------------------------------------------------------------------
// HTTP helper
// ---------------------------------------------------------------------------

export function httpJson(
  method: string,
  url: string,
  body?: Record<string, unknown>,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  return httpRaw(method, url, "application/json", body ? JSON.stringify(body) : undefined, headers);
}

/** OAuth 2.1 token endpoint: application/x-www-form-urlencoded body. */
export function httpForm(
  method: string,
  url: string,
  form: Record<string, string>,
  headers?: Record<string, string>,
): Promise<{ status: number; data: any }> {
  const body = new URLSearchParams(form).toString();
  return httpRaw(method, url, "application/x-www-form-urlencoded", body, headers);
}

function httpRaw(
  method: string,
  url: string,
  contentType: string,
  body: string | undefined,
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
        "Content-Type": contentType,
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
    if (body) req.write(body);
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Token manager with auto-refresh
// ---------------------------------------------------------------------------

interface TokenState {
  access_token: string;
  expires_at: number; // epoch ms
}

let tokenState: TokenState | null = null;

// Single-flight gate. Concurrent callers that arrive while a token
// issue is in progress await the same in-flight promise instead of
// each issuing their own /oauth/token call. The plugin issues many
// concurrent API calls (pullEvents, publishEvent, agentrux_upload) on
// the same shared tokenState; without coalescing they would each
// hammer /oauth/token in lock-step and burn the rate limit.
//
// Note: the OAuth 2.1 ``client_credentials`` grant does NOT issue a
// refresh_token (RFC 6749 §4.4 + AgenTrux spec §22.2). The plugin runs
// as a script credential, so the only refresh path here is "discard
// expired access_token, mint a fresh one with client_id+secret". The
// previous implementation kept a /auth/refresh branch for the
// short-lived AC era; that branch and the endpoint behind it are gone.
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
      // Issue (or re-issue) via OAuth 2.1 client_credentials grant.
      // POST /oauth/token, form-encoded; client_id has the `script_<UUID>`
      // namespace prefix that distinguishes a Script credential from a
      // DCR-issued OAuth client.
      const r = await httpForm("POST", `${creds.base_url}/oauth/token`, {
        grant_type: "client_credentials",
        client_id: `script_${creds.script_id}`,
        client_secret: creds.clientSecret,
      });
      if (r.status !== 200) {
        throw new Error(`Auth failed: ${JSON.stringify(r.data)}`);
      }
      tokenState = {
        access_token: r.data.access_token,
        expires_at: oauthExpiresAt(r.data.expires_in),
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

/** OAuth `expires_in` is seconds-from-now; convert to absolute epoch ms. */
function oauthExpiresAt(ei: unknown): number {
  if (typeof ei === "number" && Number.isFinite(ei) && ei > 0) {
    return Date.now() + ei * 1000;
  }
  return Date.now() + 3600_000;
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

export async function pullEvents(
  creds: Credentials,
  topicId: string,
  afterSeq: number,
  limit = 20,
): Promise<any[]> {
  const result = await authRequest(
    creds,
    "GET",
    `/topics/${topicId}/events?after_sequence_no=${afterSeq}&limit=${limit}`,
  );
  return result.items || [];
}

export async function publishEvent(
  creds: Credentials,
  topicId: string,
  eventType: string,
  payload: Record<string, unknown>,
): Promise<string> {
  const result = await authRequest(creds, "POST", `/topics/${topicId}/events`, {
    type: eventType,
    payload,
  });
  return result.event_id;
}

/**
 * Upload a file to AgenTrux via presigned URL.
 * Returns { object_id, download_url } for attaching to events.
 */
export async function uploadFile(
  creds: Credentials,
  topicId: string,
  filePath: string,
  contentType: string,
): Promise<{ object_id: string; download_url: string }> {
  const fs = await import("fs");
  const path = await import("path");
  const data = fs.readFileSync(filePath);
  const filename = path.basename(filePath);

  // 1. Get presigned upload URL
  const info = await authRequest(creds, "POST", `/topics/${topicId}/payloads`, {
    content_type: contentType,
    filename,
    size: data.length,
  });

  // 2. Upload file to presigned URL
  const uploadUrl = new URL(info.upload_url);
  const mod = uploadUrl.protocol === "https:" ? await import("https") : await import("http");

  await new Promise<void>((resolve, reject) => {
    const req = mod.request(
      {
        hostname: uploadUrl.hostname,
        port: uploadUrl.port,
        path: uploadUrl.pathname + uploadUrl.search,
        method: "PUT",
        headers: { "Content-Type": contentType, "Content-Length": data.length },
      },
      (res) => {
        res.resume();
        res.on("end", () => {
          if (res.statusCode && res.statusCode < 300) resolve();
          else reject(new Error(`Upload failed: ${res.statusCode}`));
        });
      },
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });

  return {
    object_id: info.object_id,
    download_url: info.download_url || `${creds.base_url}/topics/${topicId}/payloads/${info.object_id}`,
  };
}
