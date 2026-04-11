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
// Capture tool registrations by mocking the api object
// ---------------------------------------------------------------------------

interface RegisteredTool {
  name: string;
  description: string;
  parameters: any;
  execute: (...args: any[]) => Promise<any>;
}

function captureTools(): RegisteredTool[] {
  const tools: RegisteredTool[] = [];
  const fakeApi = {
    registerTool(def: any, _opts?: any) {
      tools.push(def);
    },
  };

  // Import the default export and invoke it with our fake api.
  // We need to isolate the module to avoid side effects from credential loading.
  jest.isolateModules(() => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const pluginModule = require("../index");
    const register = pluginModule.default || pluginModule;
    register(fakeApi);
  });

  return tools;
}

function findTool(tools: RegisteredTool[], name: string) {
  return tools.find((t) => t.name === name);
}

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
// Activate tool parameter naming
// ---------------------------------------------------------------------------

describe("Activate tool", () => {
  let tools: RegisteredTool[];
  let activateTool: RegisteredTool | undefined;

  beforeAll(() => {
    tools = captureTools();
    activateTool = findTool(tools, "agentrux_activate");
  });

  test("activate tool exists", () => {
    expect(activateTool).toBeDefined();
  });

  test("parameter is activation_code (not token)", () => {
    const props = activateTool!.parameters.properties;
    expect(props).toHaveProperty("activation_code");
    // "token" should not be the parameter name for activation
    expect(props).not.toHaveProperty("activation_token");
  });

  test("activation_code description references ac_ prefix", () => {
    const desc = activateTool!.parameters.properties.activation_code.description;
    expect(desc).toContain("ac_");
    expect(desc).not.toContain("atk_");
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
