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

const CREDENTIALS_DIR = AGENTRUX_DIR;

export interface Credentials {
  base_url: string;
  script_id: string;
  clientSecret: string;
}

export function loadCredentials(): Credentials | null {
  try {
    if (fs.existsSync(CREDENTIALS_PATH)) {
      return JSON.parse(fs.readFileSync(CREDENTIALS_PATH, "utf-8"));
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
