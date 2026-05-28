// RFC 8628 §3.4 device flow polling primitive (shared).
//
// SSOT: docs/04_design/auth/device_code_setup_v1.md §5-1 (Step 1a 抽出のみ、 挙動不変)
//
// `topology-install.ts` の pollToken() から polling loop 部分のみ抽出。
// 返り値は raw 200 body (Record<string, unknown>)、 caller が flow 別 parse する設計:
//   - Topology Flow v1: topology-install._parseTokenResponse() で InstallResult
//   - Plain device code: device-code-setup._parseTokenResponse() で DeviceCodeSetupResult
//
// Step 1b (別 PR) で jitter / 429 Retry-After / connect retry を追加する余地を残しつつ、
// v1 は 既存 jest test (28 件) が **変更なしで pass** する pure-move とする。

import {
  InstallAuthError,
  InstallDeniedError,
  InstallError,
  InstallTimeoutError,
  parseOAuthError,
} from "./oauth-errors";

// Polling constants (RFC 8628 §3.5 + AgenTrux server 整合)
export const MIN_POLL_INTERVAL_MS = 1000;
export const MAX_POLL_INTERVAL_MS = 60_000;
export const SLOW_DOWN_INCREMENT_MS = 5_000;

// Endpoint constants (本 module 内 helper 用、 外部 export しない)
const TOKEN_PATH = "/oauth/token";
const GRANT_TYPE_DEVICE_CODE = "device_code";
const USER_AGENT = "agentrux-sdk-oauth-polling-ts/1.0";

export interface PollDeviceTokenArgs {
  base: string; // base URL (e.g. "https://api.agentrux.com")、 trailing slash なし
  clientId: string;
  deviceCode: string;
  userCode: string; // error message 用 (raw bearer の device_code を漏らさない)
  timeoutS: number; // 全体 deadline 秒 (caller が ≤600 を保証)
  initialIntervalSeconds: number; // server `interval` 値
  fetchImpl: typeof fetch;
  signal?: AbortSignal;
  /** custom delay (test 用、 default は setTimeout-based) */
  delayImpl?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/**
 * RFC 8628 §3.4 polling loop。 200 成功時に raw body (Record<string, unknown>) を返す.
 *
 * @throws InstallTimeoutError - timeout 超過 or RFC 8628 expired_token
 * @throws InstallDeniedError - RFC 8628 access_denied
 * @throws InstallAuthError - RFC 8628 invalid_client
 * @throws InstallError - invalid_grant 系 / unexpected error
 */
export async function pollDeviceToken(
  args: PollDeviceTokenArgs,
): Promise<Record<string, unknown>> {
  const deadlineMs = Date.now() + args.timeoutS * 1000;
  let intervalMs = Math.max(
    MIN_POLL_INTERVAL_MS,
    args.initialIntervalSeconds * 1000,
  );
  const delayFn = args.delayImpl ?? delay;

  // biome-ignore lint/correctness/noUnreachable: while(true) terminates via return/throw
  while (true) {
    if (Date.now() >= deadlineMs) {
      throw new InstallTimeoutError(
        `approval not completed within ${args.timeoutS}s (user_code=${args.userCode})`,
      );
    }
    await delayFn(intervalMs, args.signal);

    const form = new URLSearchParams();
    form.set("grant_type", GRANT_TYPE_DEVICE_CODE);
    form.set("device_code", args.deviceCode);
    form.set("client_id", args.clientId);

    const res = await safeFetch(args.fetchImpl, `${args.base}${TOKEN_PATH}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": USER_AGENT,
        Accept: "application/json",
      },
      body: form.toString(),
      signal: args.signal,
    });

    if (res.status === 200) {
      return (await res.json()) as Record<string, unknown>;
    }

    const { code, desc } = await parseOAuthError(res);
    if (code === "authorization_pending") continue;
    if (code === "slow_down") {
      intervalMs = Math.min(
        MAX_POLL_INTERVAL_MS,
        intervalMs + SLOW_DOWN_INCREMENT_MS,
      );
      continue;
    }
    if (code === "access_denied") {
      throw new InstallDeniedError(`user denied (user_code=${args.userCode})`);
    }
    if (code === "expired_token") {
      throw new InstallTimeoutError(
        `device_code expired (user_code=${args.userCode})`,
      );
    }
    if (code === "invalid_grant") {
      throw new InstallError(`invalid_grant: ${desc}`, code, res.status);
    }
    if (code === "invalid_client") {
      throw new InstallAuthError(`invalid_client: ${desc}`);
    }
    throw new InstallError(
      `unexpected token response status=${res.status} error=${code || "?"} desc=${desc}`,
      code,
      res.status,
    );
  }
}

/**
 * fetch wrapper: network error を InstallError でラップして OAuth-shaped error 階層に統一.
 */
export async function safeFetch(
  fetchImpl: typeof fetch,
  url: string,
  init: RequestInit,
): Promise<Response> {
  try {
    return await fetchImpl(url, init);
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e);
    throw new InstallError(`network error: ${msg}`);
  }
}

/**
 * setTimeout-based delay with AbortSignal 対応.
 * AbortSignal 発火時は InstallAbortedError を reject する (動的 import で循環依存回避)。
 */
export async function delay(ms: number, signal?: AbortSignal): Promise<void> {
  // Local import to avoid module-level circular reference if oauth-errors later
  // imports from this module.
  const { InstallAbortedError } = await import("./oauth-errors");
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new InstallAbortedError());
      return;
    }
    const t = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    const onAbort = () => {
      cleanup();
      reject(new InstallAbortedError());
    };
    signal?.addEventListener("abort", onAbort, { once: true });
    function cleanup() {
      clearTimeout(t);
      signal?.removeEventListener("abort", onAbort);
    }
  });
}
