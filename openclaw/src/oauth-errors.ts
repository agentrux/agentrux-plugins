// OAuth (RFC 6749 / RFC 8628) error parsing + Install* error hierarchy.
//
// SSOT: docs/04_design/auth/device_code_setup_v1.md §5-1 (Step 1a 抽出のみ、 挙動不変)
//
// 本 module は `topology-install.ts` から error parsing + 階層を pure-move したもの。
// `device-code-setup.ts` (Step 3) も同じ Install* 階層を再利用する (1 catch で両 flow 扱える)。

export class InstallError extends Error {
  constructor(
    message: string,
    public errorCode?: string,
    public httpStatus?: number,
  ) {
    super(message);
    this.name = "InstallError";
  }
}

export class InstallDeniedError extends InstallError {
  constructor(message = "user denied the request") {
    super(message, "access_denied");
    this.name = "InstallDeniedError";
  }
}

export class InstallTimeoutError extends InstallError {
  constructor(message = "approval did not complete within timeout") {
    super(message, "expired_token");
    this.name = "InstallTimeoutError";
  }
}

export class InstallConfigError extends InstallError {
  constructor(message: string) {
    super(message, "invalid_config");
    this.name = "InstallConfigError";
  }
}

export class InstallAuthError extends InstallError {
  constructor(message: string) {
    super(message, "invalid_client");
    this.name = "InstallAuthError";
  }
}

// Codex round 1 MF-4 (TS): AbortSignal による中断を専用 error 型で表現
// (Python の InstallAbortedError と symmetric)。
export class InstallAbortedError extends InstallError {
  constructor(message = "aborted by signal") {
    super(message, "aborted");
    this.name = "InstallAbortedError";
  }
}

/**
 * OAuth error response body から (code, desc) を取り出す.
 *
 * FastAPI HTTPException shape `{ detail: { error, error_description } }` と
 * RFC 6749 flat shape `{ error, error_description }` の両方を受け入れる。
 * parse 失敗時は code="" + desc=<short snippet> を返す (caller が status_code でフォールバック判定)。
 */
export async function parseOAuthError(
  res: Response,
): Promise<{ code: string; desc: string }> {
  try {
    const body = (await res.json()) as Record<string, unknown>;
    if (body && typeof body === "object") {
      // FastAPI nested
      const detail = body.detail;
      if (detail && typeof detail === "object") {
        const d = detail as Record<string, unknown>;
        return {
          code: String(d.error ?? ""),
          desc: String(d.error_description ?? ""),
        };
      }
      // OAuth flat
      return {
        code: String(body.error ?? ""),
        desc: String(body.error_description ?? ""),
      };
    }
    return { code: "", desc: JSON.stringify(body).slice(0, 200) };
  } catch {
    return { code: "", desc: `<non-json status=${res.status}>` };
  }
}
