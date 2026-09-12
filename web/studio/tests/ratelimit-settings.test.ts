import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET, PUT } from "@/app/api/settings/ratelimit/route";
import { POST as generatePost } from "@/app/api/generate/route";
import {
  DEFAULT_RATE_LIMIT_PER_MIN,
  MAX_RATE_LIMIT_PER_MIN,
  RATE_LIMIT_ENV,
  checkRateLimit,
  rateLimitOverridePath,
  readRateLimitOverride,
  readRateLimitState,
  resetRateLimits,
  writeRateLimitOverride,
} from "@/lib/ratelimit";

// The routes call loadRepoEnv(), which reads the repo-root .env; `delete
// process.env[k]` is not isolation because the loader puts it back from disk.
// Every test here sets exactly the variables it needs. (env.test.ts covers the
// loader itself.)
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

/**
 * The runtime override is stored, so each test owns a directory under the
 * gitignored tests/.tmp. Nothing here touches a real Studio data directory.
 */
const TMP = join(process.cwd(), "tests", ".tmp", "ratelimit");

// The OIDC keys are cleared too: this tool shell exports a real issuer and
// client secret, and a leftover export would turn the auth gate on and answer
// 401 before the assertions under test ever run. Each test sets exactly what it
// needs, so nothing here depends on the calling environment.
const MANAGED = [
  RATE_LIMIT_ENV,
  "STUDIO_DATA_DIR",
  "STUDIO_ACCESS_TOKEN",
  "OMNIROUTE_API_KEY",
  "OMNIROUTE_BASE_URL",
  "OMNIROUTE_MODEL",
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_SCOPES",
  "OIDC_ALLOWED_GROUPS",
  "STUDIO_SESSION_SECRET",
] as const;

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  rmSync(TMP, { recursive: true, force: true });
  process.env.STUDIO_DATA_DIR = TMP;
  resetRateLimits();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(TMP, { recursive: true, force: true });
  vi.unstubAllGlobals();
  resetRateLimits();
});

/* ---- the stored override ------------------------------------------------ */

describe("the stored override", () => {
  it("is absent until something sets it", () => {
    expect(readRateLimitOverride()).toBeNull();
    expect(readRateLimitState()).toMatchObject({
      limit: DEFAULT_RATE_LIMIT_PER_MIN,
      source: "default",
      override: null,
    });
  });

  it("round-trips a limit through disk", () => {
    writeRateLimitOverride(30);

    // Read from the file, not from memory: a reload must see the same setting.
    expect(readRateLimitOverride()).toBe(30);
    expect(JSON.parse(readFileSync(rateLimitOverridePath(), "utf8"))).toEqual({ perMinute: 30 });
    expect(readRateLimitState()).toMatchObject({ limit: 30, source: "override", override: 30 });
  });

  it("removes the file when cleared, so the deployment default resumes", () => {
    process.env[RATE_LIMIT_ENV] = "7";
    writeRateLimitOverride(2);
    expect(readRateLimitState().limit).toBe(2);

    writeRateLimitOverride(null);
    expect(readRateLimitOverride()).toBeNull();
    expect(readRateLimitState()).toMatchObject({ limit: 7, source: "env", override: null });
  });

  it("refuses a value that is not a whole number of generations", () => {
    expect(() => writeRateLimitOverride(-1)).toThrow(RangeError);
    expect(() => writeRateLimitOverride(1.5)).toThrow(RangeError);
    expect(readRateLimitOverride()).toBeNull();
  });

  it("caps a value above the maximum instead of storing it", () => {
    writeRateLimitOverride(MAX_RATE_LIMIT_PER_MIN + 5_000);
    expect(readRateLimitOverride()).toBe(MAX_RATE_LIMIT_PER_MIN);
  });
});

/* ---- how the two layers combine ---------------------------------------- */

describe("the override and the environment", () => {
  it("lets the override win over .env", () => {
    process.env[RATE_LIMIT_ENV] = "3";
    writeRateLimitOverride(40);
    expect(readRateLimitState()).toMatchObject({ limit: 40, source: "override", envLimit: 3 });
  });

  it("disables the limit with an override of 0 even when .env sets one", () => {
    process.env[RATE_LIMIT_ENV] = "3";
    writeRateLimitOverride(0);

    expect(readRateLimitState()).toMatchObject({ limit: null, source: "override", override: 0 });
    for (let index = 0; index < 25; index += 1) {
      expect(checkRateLimit("an-account").ok).toBe(true);
    }
  });

  it("reports .env as the source when only .env decides", () => {
    process.env[RATE_LIMIT_ENV] = "off";
    expect(readRateLimitState()).toMatchObject({ limit: null, source: "env", override: null });
  });

  it("ignores an unreadable or nonsense override file rather than failing", () => {
    mkdirSync(TMP, { recursive: true, mode: 0o700 });

    writeFileSync(rateLimitOverridePath(), "{ not json");
    expect(readRateLimitOverride()).toBeNull();
    expect(readRateLimitState().limit).toBe(DEFAULT_RATE_LIMIT_PER_MIN);

    writeFileSync(rateLimitOverridePath(), JSON.stringify({ perMinute: -5 }));
    expect(readRateLimitOverride()).toBeNull();

    writeFileSync(rateLimitOverridePath(), JSON.stringify({ perMinute: "20" }));
    expect(readRateLimitOverride()).toBeNull();

    writeFileSync(rateLimitOverridePath(), JSON.stringify({}));
    expect(readRateLimitOverride()).toBeNull();
  });

  it("enforces a stored limit in the counter", () => {
    writeRateLimitOverride(2);
    const identity = `acct-${Math.random()}`;

    expect(checkRateLimit(identity).ok).toBe(true);
    expect(checkRateLimit(identity).ok).toBe(true);
    expect(checkRateLimit(identity).ok).toBe(false);

    // Lifting the limit — not just setting it — takes effect immediately.
    writeRateLimitOverride(null);
    expect(checkRateLimit(identity).ok).toBe(true);
  });
});

/* ---- the settings endpoint --------------------------------------------- */

describe("GET /api/settings/ratelimit", () => {
  it("reports the effective limit and where it came from", async () => {
    process.env[RATE_LIMIT_ENV] = "5";
    const response = await GET(new Request("http://studio.test/api/settings/ratelimit"));

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect((await response.json()).rateLimit).toMatchObject({
      limit: 5,
      source: "env",
      envRaw: "5",
      max: MAX_RATE_LIMIT_PER_MIN,
    });
  });

  it("is gated like every other route", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    const denied = await GET(new Request("http://studio.test/api/settings/ratelimit"));
    expect(denied.status).toBe(401);

    const allowed = await GET(
      new Request("http://studio.test/api/settings/ratelimit", {
        headers: { "x-studio-token": "shared-secret" },
      }),
    );
    expect(allowed.status).toBe(200);
  });
});

describe("PUT /api/settings/ratelimit", () => {
  function put(body: unknown, headers: Record<string, string> = {}): Promise<Response> {
    return PUT(
      new Request("http://studio.test/api/settings/ratelimit", {
        method: "PUT",
        headers: { "content-type": "application/json", ...headers },
        body: typeof body === "string" ? body : JSON.stringify(body),
      }),
    );
  }

  it("switches the limit off", async () => {
    process.env[RATE_LIMIT_ENV] = "5";
    const response = await put({ disabled: true });

    expect(response.status).toBe(200);
    expect((await response.json()).rateLimit).toMatchObject({ limit: null, override: 0 });
    expect(readRateLimitOverride()).toBe(0);
  });

  it("switches it back on with a given limit", async () => {
    await put({ disabled: true });
    const response = await put({ disabled: false, perMinute: 12 });

    expect(response.status).toBe(200);
    expect((await response.json()).rateLimit).toMatchObject({ limit: 12, source: "override" });
  });

  it("clears the override back to the deployment default", async () => {
    process.env[RATE_LIMIT_ENV] = "9";
    await put({ disabled: true });

    const response = await put({ reset: true });
    expect((await response.json()).rateLimit).toMatchObject({ limit: 9, source: "env", override: null });
    expect(readRateLimitOverride()).toBeNull();
  });

  it("rejects a request that does not say what to do", async () => {
    expect((await put({ perMinute: 10 })).status).toBe(400);
    expect((await put("not json")).status).toBe(400);
  });

  it("rejects an impossible limit rather than guessing", async () => {
    expect((await put({ disabled: false, perMinute: 0 })).status).toBe(400);
    expect((await put({ disabled: false, perMinute: -5 })).status).toBe(400);
    expect((await put({ disabled: false, perMinute: 2.5 })).status).toBe(400);
    expect((await put({ disabled: false })).status).toBe(400);
    expect((await put({ disabled: false, perMinute: MAX_RATE_LIMIT_PER_MIN + 1 })).status).toBe(413);
    // Nothing was stored by any of those.
    expect(readRateLimitOverride()).toBeNull();
  });

  it("is gated like every other route", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    expect((await put({ disabled: true })).status).toBe(401);
    expect(readRateLimitOverride()).toBeNull();
  });
});

/* ---- the point of the setting: it changes real generation --------------- */

describe("the override governs /api/generate", () => {
  const OK_STREAM = ['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"];

  function generate(headers: Record<string, string> = {}): Promise<Response> {
    return generatePost(
      new Request("http://studio.test/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json", ...headers },
        body: JSON.stringify({ prompt: "a counter" }),
      }),
    );
  }

  beforeEach(() => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(OK_STREAM.join(""), {
            status: 200,
            headers: { "content-type": "text/event-stream" },
          }),
      ),
    );
  });

  it("refuses past a limit set from the settings endpoint", async () => {
    await PUT(
      new Request("http://studio.test/api/settings/ratelimit", {
        method: "PUT",
        headers: { "content-type": "application/json", "x-studio-token": "shared-secret" },
        body: JSON.stringify({ disabled: false, perMinute: 1 }),
      }),
    );

    const headers = { "x-studio-token": "shared-secret" };
    expect((await generate(headers)).status).toBe(200);
    const throttled = await generate(headers);
    expect(throttled.status).toBe(429);
    expect(Number(throttled.headers.get("retry-after"))).toBeGreaterThan(0);
  });

  it("lets generation run uncapped once the limit is switched off, even over .env", async () => {
    process.env[RATE_LIMIT_ENV] = "1";
    writeRateLimitOverride(0);

    const headers = { "x-studio-token": "shared-secret" };
    for (let index = 0; index < 5; index += 1) {
      expect((await generate(headers)).status).toBe(200);
    }
  });
});
