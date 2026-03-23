/**
 * Token naming correctness tests for the Flowise plugin.
 *
 * Verifies that AgenTruxCredential uses the final unified naming:
 * clientSecret, inviteCode. Ensures no legacy names (secret, grantToken,
 * grant_token, example.com) remain.
 */

// Flowise credentials use module.exports = { credClass: ... }
// eslint-disable-next-line @typescript-eslint/no-var-requires
const credModule = require("../AgenTruxCredential");

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function inputNames(cred: any): string[] {
  return cred.inputs.map((i: any) => i.name);
}

function findInput(cred: any, name: string) {
  return cred.inputs.find((i: any) => i.name === name);
}

// ---------------------------------------------------------------------------
// AgenTruxCredential definition
// ---------------------------------------------------------------------------

describe("AgenTruxCredential", () => {
  let cred: any;

  beforeAll(() => {
    const CredClass = credModule.credClass;
    // credClass is already an instance in the module.exports pattern used
    // by flowise — but AgenTruxCredential uses a class, so the export is
    // the class itself. Instantiate if it's a constructor.
    if (typeof CredClass === "function") {
      cred = new CredClass();
    } else {
      cred = CredClass;
    }
  });

  // -- clientSecret --

  test("credential inputs include clientSecret (not secret)", () => {
    const names = inputNames(cred);
    expect(names).toContain("clientSecret");
    // Must not have a bare "secret" field name
    expect(names).not.toContain("secret");
  });

  // -- inviteCode --

  test("credential inputs include inviteCode (not grantToken)", () => {
    const names = inputNames(cred);
    expect(names).toContain("inviteCode");
    expect(names).not.toContain("grantToken");
    expect(names).not.toContain("grant_token");
  });

  // -- placeholder URL --

  test("placeholder URL uses api.agentrux.com", () => {
    const baseUrlInput = findInput(cred, "baseUrl");
    expect(baseUrlInput).toBeDefined();
    expect(baseUrlInput.placeholder).toContain("api.agentrux.com");
    expect(baseUrlInput.placeholder).not.toContain("example.com");
  });

  // -- no legacy names anywhere in serialized inputs --

  test("no legacy names in input definitions", () => {
    const serialized = JSON.stringify(cred.inputs);
    expect(serialized).not.toContain('"grantToken"');
    expect(serialized).not.toContain('"grant_token"');
    expect(serialized).not.toContain('"activationToken"');
    expect(serialized).not.toContain("atk_");
    expect(serialized).not.toContain("gtk_");
    expect(serialized).not.toContain("example.com");
  });
});
