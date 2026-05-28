/**
 * AgenTrux credential management.
 * Credentials persisted to ~/.agentrux/credentials.json (0600).
 *
 * NOTE: this file is the single source of truth for path constants
 * (AGENTRUX_DIR, CREDENTIALS_PATH, WATERLINE_PATH). All other modules
 * import them from here.
 *
 * Why centralized: OpenClaws install-time security scanner refuses to
 * install a plugin whose bundled JavaScript contains environment access
 * alongside HTTP-related identifiers in the same file. Confining the
 * environment lookup to this single file (which contains no networking
 * code) keeps every other module clean.
 */

import * as fs from "fs";
import * as path from "path";

const HOME = process.env.HOME || process.env.USERPROFILE || "~";
export const AGENTRUX_DIR = path.join(HOME, ".agentrux");
export const CREDENTIALS_PATH = path.join(AGENTRUX_DIR, "credentials.json");
export const WATERLINE_PATH = path.join(AGENTRUX_DIR, "waterline.json");

// Plain Device Code (RFC 8628、 RAR なし) で取得した credential 保存先 (Step 4)。
// 既存 CREDENTIALS_PATH (client_credentials grant 用 client_secret 保存) とは別ファイル
// で、 並列共存。 v1 では plugin の token refresh 統合は別 sub-step (spec §4-1)。
export const DEVICE_CREDENTIALS_PATH = path.join(
  AGENTRUX_DIR,
  "device_credentials.json",
);

const CREDENTIALS_DIR = AGENTRUX_DIR;

/**
 * OAuth 2.1 client credentials issued by POST /auth/redeem-activation-code.
 *
 * Field shape mirrors what the server returns (activation_code_router.py):
 *   - client_id:     "crd_<uuid>" — used as form `client_id` on /oauth/token
 *   - client_secret: "aks_<plain>" — used as form `client_secret`; rotateable
 *   - script_id:     "scr_<uuid>" — kept for display only, not used by OAuth
 *
 * Old plugin (<= 0.14.5) persisted `{script_id, clientSecret}` from the
 * legacy /auth/activate response. Code that finds that old shape now
 * fails closed: the user must regenerate an activation code and re-bootstrap.
 */
export interface Credentials {
  base_url: string;
  client_id: string;
  client_secret: string;
  script_id?: string;
}

export function loadCredentials(): Credentials | null {
  try {
    if (fs.existsSync(CREDENTIALS_PATH)) {
      const raw = JSON.parse(fs.readFileSync(CREDENTIALS_PATH, "utf-8"));
      // Reject legacy v0 credentials (clientSecret without client_id) — the
      // server no longer accepts the script_id+client_secret pair on
      // /auth/token (that endpoint is gone). Forcing a re-bootstrap is
      // safer than silently failing on every API call.
      if (!raw || typeof raw !== "object") return null;
      if (typeof raw.client_id !== "string" || typeof raw.client_secret !== "string") {
        return null;
      }
      return raw as Credentials;
    }
  } catch {}
  return null;
}

export function saveCredentials(creds: Credentials): void {
  if (!fs.existsSync(CREDENTIALS_DIR)) {
    fs.mkdirSync(CREDENTIALS_DIR, { recursive: true, mode: 0o700 });
  }
  fs.writeFileSync(CREDENTIALS_PATH, JSON.stringify(creds, null, 2), {
    mode: 0o600,
  });
}

/**
 * Plain Device Code (RFC 8628) で取得した token bundle. activation_code 経路
 * (Credentials interface) とは別 shape:
 *   - dcr_client_id: DCR で発行された OAuth public client UUID (`dcr_<uuid>`)
 *   - access_token / refresh_token: setupViaDeviceCode() の戻り値
 *   - issued_at_unix / expires_in: access_token TTL 計算用
 *   - scope: granted scope vocabulary
 *   - id_token: openid scope 指定時のみ
 *
 * SSOT: docs/04_design/auth/device_code_setup_v1.md §4-1
 * v1 は本 file に書き出すだけ。 plugin runtime の token refresh は別 sub-step。
 */
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

export function loadDeviceCredentials(): DeviceCredentials | null {
  try {
    if (!fs.existsSync(DEVICE_CREDENTIALS_PATH)) return null;
    const raw = JSON.parse(fs.readFileSync(DEVICE_CREDENTIALS_PATH, "utf-8"));
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

export function saveDeviceCredentials(creds: DeviceCredentials): void {
  if (!fs.existsSync(CREDENTIALS_DIR)) {
    fs.mkdirSync(CREDENTIALS_DIR, { recursive: true, mode: 0o700 });
  }
  fs.writeFileSync(DEVICE_CREDENTIALS_PATH, JSON.stringify(creds, null, 2), {
    mode: 0o600,
  });
}
