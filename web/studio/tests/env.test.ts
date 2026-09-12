import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The loader walks up from cwd and stops at the repository root. Fixtures live
 * inside the project (tests/.tmp, gitignored) with their own .git marker, and a
 * decoy .env above that boundary to prove the walk stops.
 */

const ORIGINAL_CWD = process.cwd();
const TMP = join(ORIGINAL_CWD, "tests", ".tmp");
const SANDBOX = join(TMP, "env");
const APP_DIR = join(SANDBOX, "repo", "app");

const FIXTURE_KEYS = [
  "FOO",
  "ROOT_ONLY",
  "APP_ONLY",
  "APP_LOCAL_ONLY",
  "QUOTED",
  "SINGLE_QUOTED",
  "INLINE",
  "FROM_EXAMPLE",
  "ABOVE_BOUNDARY",
  "FROM_SHELL",
];

function write(relative: string, contents: string): void {
  const target = join(SANDBOX, relative);
  mkdirSync(join(target, ".."), { recursive: true });
  writeFileSync(target, contents);
}

beforeAll(() => {
  rmSync(SANDBOX, { recursive: true, force: true });

  // Decoy above the boundary — must never be read.
  write(".env", "ABOVE_BOUNDARY=leaked\n");

  // Repository boundary marker.
  mkdirSync(join(SANDBOX, "repo", ".git"), { recursive: true });

  write(
    "repo/.env",
    [
      "# comment line",
      "FOO=from-root",
      "ROOT_ONLY=yes",
      'QUOTED="quoted value"',
      "SINGLE_QUOTED='single value'",
      "INLINE=clean  # trailing note",
      "=notakey",
      "BAD LINE",
      "9LEADING_DIGIT=nope",
      "export FROM_EXAMPLE=should-not-load",
    ].join("\n"),
  );

  write("repo/.env.example", "FROM_EXAMPLE=should-not-load\n");
  write("repo/app/.env", "FOO=from-app\nAPP_ONLY=yes\n");
  write("repo/app/.env.local", "APP_LOCAL_ONLY=yes\n");
});

afterAll(() => {
  rmSync(TMP, { recursive: true, force: true });
});

beforeEach(() => {
  for (const key of FIXTURE_KEYS) delete process.env[key];
  process.chdir(APP_DIR);
});

afterEach(() => {
  process.chdir(ORIGINAL_CWD);
});

/** loadRepoEnv runs once per module instance, so each test gets a fresh one. */
async function freshLoader() {
  vi.resetModules();
  return (await import("@/lib/env")).loadRepoEnv;
}

describe("loadRepoEnv", () => {
  it("adopts the nearest .env and merges files from ancestor directories", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.FOO).toBe("from-app");
    expect(process.env.ROOT_ONLY).toBe("yes");
    expect(process.env.APP_ONLY).toBe("yes");
  });

  it("reads .env.local alongside .env", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.APP_LOCAL_ONLY).toBe("yes");
  });

  it("stops at the repository root and never reads a parent workspace's env", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.ABOVE_BOUNDARY).toBeUndefined();
  });

  it("never loads .env.example", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.FROM_EXAMPLE).toBeUndefined();
  });

  it("does not overwrite values already present in the process env", async () => {
    process.env.FROM_SHELL = "from-shell";
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.FROM_SHELL).toBe("from-shell");
    expect(process.env.ROOT_ONLY).toBe("yes");
  });

  it("keeps an explicit empty value rather than adopting the file's", async () => {
    process.env.ROOT_ONLY = "";
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.ROOT_ONLY).toBe("");
  });

  it("unwraps quoted values and strips inline comments", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();

    expect(process.env.QUOTED).toBe("quoted value");
    expect(process.env.SINGLE_QUOTED).toBe("single value");
    expect(process.env.INLINE).toBe("clean");
  });

  it("skips malformed lines instead of throwing", async () => {
    const loadRepoEnv = await freshLoader();
    expect(() => loadRepoEnv()).not.toThrow();
    expect(process.env.FOO).toBe("from-app");
  });

  it("is idempotent", async () => {
    const loadRepoEnv = await freshLoader();
    loadRepoEnv();
    process.env.FOO = "mutated";
    loadRepoEnv();

    expect(process.env.FOO).toBe("mutated");
  });
});
