/**
 * Token naming correctness tests for the n8n plugin.
 *
 * Verifies that credential definitions and transport types use the final
 * unified naming: clientSecret, inviteCode, activationCode, ac_, inv_.
 * Ensures no legacy names (secret, grant_token, grantToken, atk_, gtk_,
 * your-org, example.com) leak into the public surface.
 */

import { AgenTruxApi } from "../credentials/AgenTruxApi.credentials";
import {
  ActivationResult,
  ResolvedCredentials,
} from "../transport/apiRequest";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function credentialPropertyNames(cred: AgenTruxApi): string[] {
  return cred.properties.map((p) => p.name);
}

function findProperty(cred: AgenTruxApi, name: string) {
  return cred.properties.find((p) => p.name === name);
}

// ---------------------------------------------------------------------------
// AgenTruxApi credential definition
// ---------------------------------------------------------------------------

describe("AgenTruxApi credential", () => {
  let cred: AgenTruxApi;

  beforeAll(() => {
    cred = new AgenTruxApi();
  });

  test("has a name property", () => {
    expect(cred.name).toBeDefined();
    expect(typeof cred.name).toBe("string");
    expect(cred.name.length).toBeGreaterThan(0);
  });

  // -- clientSecret --

  test("properties include clientSecret field (not secret)", () => {
    const names = credentialPropertyNames(cred);
    expect(names).toContain("clientSecret");
    // Must not have a bare "secret" field
    expect(names).not.toContain("secret");
  });

  // -- inviteCode --

  test("properties include inviteCode field (not grantToken or grant_token)", () => {
    const names = credentialPropertyNames(cred);
    expect(names).toContain("inviteCode");
    expect(names).not.toContain("grantToken");
    expect(names).not.toContain("grant_token");
  });

  // -- activationCode --

  test("activationCode field exists (not activationToken)", () => {
    const names = credentialPropertyNames(cred);
    expect(names).toContain("activationCode");
    expect(names).not.toContain("activationToken");
  });

  // -- documentationUrl --

  test("documentationUrl points to https://github.com/agentrux/agentrux", () => {
    expect(cred.documentationUrl).toBe(
      "https://github.com/agentrux/agentrux",
    );
    expect(cred.documentationUrl).not.toContain("your-org");
  });

  // -- placeholder URL --

  test("placeholder URL uses api.agentrux.com (not example.com)", () => {
    const baseUrlProp = findProperty(cred, "baseUrl");
    expect(baseUrlProp).toBeDefined();
    expect(baseUrlProp!.placeholder).toContain("api.agentrux.com");
    expect(baseUrlProp!.placeholder).not.toContain("example.com");
  });

  // -- prefix conventions --

  test("activationCode placeholder uses ac_ prefix", () => {
    const prop = findProperty(cred, "activationCode");
    expect(prop).toBeDefined();
    expect(prop!.placeholder).toContain("ac_");
    expect(prop!.placeholder).not.toContain("atk_");
  });

  test("inviteCode placeholder uses inv_ prefix", () => {
    const prop = findProperty(cred, "inviteCode");
    expect(prop).toBeDefined();
    expect(prop!.placeholder).toContain("inv_");
    expect(prop!.placeholder).not.toContain("gtk_");
  });

  // -- no legacy names anywhere in stringified properties --

  test("no legacy names in property definitions", () => {
    const serialized = JSON.stringify(cred.properties);
    // Old field names
    expect(serialized).not.toContain('"grantToken"');
    expect(serialized).not.toContain('"grant_token"');
    expect(serialized).not.toContain('"activationToken"');
    // Old prefixes (as standalone tokens, not substrings)
    expect(serialized).not.toContain("atk_");
    expect(serialized).not.toContain("gtk_");
    // Old placeholder domains
    expect(serialized).not.toContain("example.com");
    expect(serialized).not.toContain("your-org");
  });
});

// ---------------------------------------------------------------------------
// Transport types
// ---------------------------------------------------------------------------

describe("Transport type naming", () => {
  test("ActivationResult has clientSecret field (not secret)", () => {
    // Verify the interface at runtime via a conforming object
    const result: ActivationResult = {
      scriptId: "test-id",
      clientSecret: "cs_test",
      grants: [],
    };
    expect(result.clientSecret).toBe("cs_test");
    expect(result).toHaveProperty("clientSecret");
    expect(result).not.toHaveProperty("secret");
  });

  test("ResolvedCredentials has clientSecret field (not secret)", () => {
    const creds: ResolvedCredentials = {
      baseUrl: "https://api.agentrux.com",
      scriptId: "test-id",
      clientSecret: "cs_test",
    };
    expect(creds.clientSecret).toBe("cs_test");
    expect(creds).toHaveProperty("clientSecret");
    expect(creds).not.toHaveProperty("secret");
  });
});
