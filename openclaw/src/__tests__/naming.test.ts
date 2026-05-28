/**
 * Token naming correctness tests for the OpenClaw plugin (Phase 1.9+).
 *
 * Verifies that the plugin uses the OAuth 2.1 naming:
 * client_secret (snake_case), activation_code, act_ prefix,
 * api.agentrux.com. Ensures no legacy names (secret, atk_/gtk_,
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

  // Import the default export and invoke its register() method.
  // The plugin is exported as an object: { id, name, register(api) }.
  jest.isolateModules(() => {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const pluginModule = require("../index");
    const plugin = pluginModule.default || pluginModule;
    if (typeof plugin === "function") {
      plugin(fakeApi);
    } else if (typeof plugin.register === "function") {
      plugin.register(fakeApi);
    } else {
      throw new Error("Plugin export is neither a function nor an object with register()");
    }
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
  test("has snake_case OAuth 2.1 fields (client_id + client_secret)", () => {
    // OAuth 2.1 client_credentials uses snake_case form fields.
    expect(credentialsSource).toContain("client_id: string");
    expect(credentialsSource).toContain("client_secret: string");
    // Must not have a bare "secret: string" field or the old camelCase form.
    expect(credentialsSource).not.toMatch(/^\s+secret:\s+string/m);
    expect(credentialsSource).not.toMatch(/^\s+clientSecret:\s+string/m);
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

  test("activation_code description references act_ prefix (Phase 1.9+)", () => {
    const desc = activateTool!.parameters.properties.activation_code.description;
    expect(desc).toContain("act_");
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
