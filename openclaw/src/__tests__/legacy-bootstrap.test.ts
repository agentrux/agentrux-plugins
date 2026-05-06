/**
 * Tests for the legacy-bootstrap quarantine helper.
 *
 * The plugin no longer activates from BOOTSTRAP.md (the /auth/activate
 * endpoint is gone). What we still owe existing users is a clear
 * signal when their old bootstrap file becomes inert — we rename it
 * to a timestamped sibling so the next gateway run doesn't trip over
 * it again, and the gateway logs why.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

let tmpHome: string;
let origHome: string | undefined;

beforeEach(() => {
  tmpHome = fs.mkdtempSync(path.join(os.tmpdir(), "agentrux-bootstrap-"));
  // os.homedir() on POSIX honours $HOME first; flipping the env var is
  // the simplest way to redirect it without monkey-patching the os
  // module (which fails under module isolation in CI).
  origHome = process.env.HOME;
  process.env.HOME = tmpHome;
  jest.resetModules();
});

afterEach(() => {
  if (origHome === undefined) {
    delete process.env.HOME;
  } else {
    process.env.HOME = origHome;
  }
  fs.rmSync(tmpHome, { recursive: true, force: true });
});

function loadModule() {
  return require("../activation-core") as typeof import("../activation-core");
}

describe("quarantineLegacyBootstrap", () => {
  test("no file → no-op", () => {
    const { quarantineLegacyBootstrap, getBootstrapPath } = loadModule();
    fs.mkdirSync(path.dirname(getBootstrapPath()), { recursive: true });
    const out = quarantineLegacyBootstrap();
    expect(out.kind).toBe("no-file");
  });

  test("BOOTSTRAP.md present → renamed to .legacy-<ts>", () => {
    const { quarantineLegacyBootstrap, getBootstrapPath } = loadModule();
    const src = getBootstrapPath();
    fs.mkdirSync(path.dirname(src), { recursive: true });
    fs.writeFileSync(src, "ac_legacy_value\n");

    const out = quarantineLegacyBootstrap();
    expect(out.kind).toBe("quarantined");
    expect(out.movedFrom).toBe(src);
    expect(out.movedTo).toMatch(/BOOTSTRAP\.md\.legacy-/);
    expect(fs.existsSync(src)).toBe(false);
    expect(fs.existsSync(out.movedTo!)).toBe(true);
    expect(fs.readFileSync(out.movedTo!, "utf8")).toBe("ac_legacy_value\n");
  });

  test("repeat call after quarantine is a no-op", () => {
    const { quarantineLegacyBootstrap, getBootstrapPath } = loadModule();
    const src = getBootstrapPath();
    fs.mkdirSync(path.dirname(src), { recursive: true });
    fs.writeFileSync(src, "stale\n");

    quarantineLegacyBootstrap();
    const second = quarantineLegacyBootstrap();
    expect(second.kind).toBe("no-file");
  });
});
