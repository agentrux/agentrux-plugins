/**
 * Token naming correctness tests for the OpenClaw plugin.
 *
 * Verifies that the plugin uses the final unified naming:
 * clientSecret, activation_code, inv_, ac_, api.agentrux.com.
 * Ensures no legacy names (secret, token as activation param,
 * process.env.HOME for credentials path) remain.
 */

import * as fs from "fs";
import * as path from "path";

// We read the source file directly because the module's default export
// is a function that registers tools on an api object — we need to
// inspect both the static source text and the runtime registrations.
const INDEX_PATH = path.resolve(__dirname, "..", "index.ts");
const CREDENTIALS_PATH = path.resolve(__dirname, "..", "credentials.ts");
const indexSource = fs.readFileSync(INDEX_PATH, "utf-8");
const credentialsSource = fs.readFileSync(CREDENTIALS_PATH, "utf-8");
// Combined source for checks that span multiple files
const source = indexSource + "\n" + credentialsSource;

// ---------------------------------------------------------------------------
// Credential interface
// ---------------------------------------------------------------------------

describe("Credentials interface", () => {
  test("has clientSecret field (not secret)", () => {
    // Check the Credentials interface definition in credentials source
    expect(credentialsSource).toContain("clientSecret: string");
    // Must not have a bare "secret: string" field in the interface
    expect(credentialsSource).not.toMatch(/^\s+secret:\s+string/m);
  });
});

// ---------------------------------------------------------------------------
// Activation flow removed
// ---------------------------------------------------------------------------

describe("Activation flow removed", () => {
  test("agentrux_activate tool no longer declared in source", () => {
    expect(indexSource).not.toContain('name: "agentrux_activate"');
    expect(indexSource).not.toContain("'agentrux_activate'");
  });

  test("source no longer calls retired activation endpoint", () => {
    // The bare ``/auth/activate`` string would mean the plugin still
    // tries to talk to the deleted endpoint. Tolerate the prose
    // mention of "activation" / "activation-code era" in comments.
    expect(indexSource).not.toMatch(/["`']\/auth\/activate["`']/);
    expect(indexSource).not.toMatch(/POST.*\/auth\/activate/);
  });
});

// ---------------------------------------------------------------------------
// Default base URL
// ---------------------------------------------------------------------------

describe("Default base URL", () => {
  test("default is https://api.agentrux.com", () => {
    // Check the source for the default base_url assignment
    expect(source).toContain("https://api.agentrux.com");
    expect(source).not.toContain("example.com");
  });
});

// ---------------------------------------------------------------------------
// Credentials path
// ---------------------------------------------------------------------------

describe("Credentials path", () => {
  test("uses .agentrux directory", () => {
    expect(credentialsSource).toContain(".agentrux");
  });
});

// ---------------------------------------------------------------------------
// No legacy names in source
// ---------------------------------------------------------------------------

describe("No legacy names in source", () => {
  test("no old token prefixes", () => {
    expect(indexSource).not.toContain("atk_");
    expect(indexSource).not.toContain("gtk_");
  });

  test("no old placeholder domains", () => {
    expect(indexSource).not.toContain("example.com");
    expect(indexSource).not.toContain("your-org");
  });

  test("invite code uses inv_ prefix", () => {
    expect(indexSource).toContain("inv_");
  });
});
