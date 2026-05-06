/**
 * Legacy-bootstrap quarantine helpers for the AgenTrux OpenClaw plugin.
 *
 * Background — what used to be here, why it's gone:
 *
 *   The plugin previously consumed a one-shot ~/.agentrux/BOOTSTRAP.md
 *   file by exchanging the activation code inside it for a
 *   (script_id, client_secret) pair via POST /auth/activate. That
 *   endpoint was retired with the OAuth 2.1 cutover (commit 5fbe7be2 in
 *   the AgenTrux backend). Calling it now is a hard 404, so any code
 *   that reaches /auth/activate is guaranteed broken.
 *
 *   Onboarding has moved to the OAuth 2.1 device flow + pre-provisioned
 *   ~/.agentrux/credentials.json (script_id + client_secret pair). The
 *   plugin no longer mints credentials on its own.
 *
 * What this module still does:
 *
 *   Some users have a stale BOOTSTRAP.md left over from the AC era. We
 *   refuse to silently ignore it — that would surface as "channel never
 *   came up" with no breadcrumb. Instead, we rename it to
 *   BOOTSTRAP.md.legacy-<ts> and log a one-line explanation so the
 *   operator knows why their bootstrap file became inert.
 *
 *   The interactive device-flow onboarding for fresh installs lives
 *   under a separate sprint (`project_oauth_greenfield_2026_05_05.md`).
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

/** ~/.agentrux/BOOTSTRAP.md — kept for the quarantine helper. */
export function getBootstrapPath(): string {
  return path.join(os.homedir(), ".agentrux", "BOOTSTRAP.md");
}

export interface QuarantineResult {
  kind: "quarantined" | "no-file";
  /** When `kind` === "quarantined": the new path the file was moved to. */
  movedTo?: string;
  /** When `kind` === "quarantined": the original path. */
  movedFrom?: string;
}

/**
 * If ~/.agentrux/BOOTSTRAP.md exists, rename it to a timestamped
 * .legacy-<ts> sibling and report what happened. Idempotent: a missing
 * file is a no-op. Errors during rename are surfaced to the caller so
 * the gateway can log them in context.
 */
export function quarantineLegacyBootstrap(): QuarantineResult {
  const src = getBootstrapPath();
  if (!fs.existsSync(src)) {
    return { kind: "no-file" };
  }
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const dst = `${src}.legacy-${ts}`;
  fs.renameSync(src, dst);
  return { kind: "quarantined", movedFrom: src, movedTo: dst };
}
