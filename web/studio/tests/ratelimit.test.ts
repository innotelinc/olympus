import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/generate/route";
import {
  DEFAULT_RATE_LIMIT_PER_MIN,
  MAX_RATE_LIMIT_PER_MIN,
  RATE_LIMIT_ENV,
  checkRateLimit,
  readRateLimitConfig,
  resetRateLimits,
} from "@/lib/ratelimit";

// The route calls loadRepoEnv(), which reads the repo-root .env. Deleting a key
// from process.env does not make it absent — the loader puts it straight back
// from the file. On a configured checkout that silently turns OIDC on and the
// auth gate answers 401 before the checks under test ever run, so the loader is
// neutralized here and every test sets exactly the variables it needs.
// (env.test.ts covers the loader itself against temp directories.)
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

const MANAGED = [
  "OMNIROUTE_API_KEY",
  "OMNIROUTE_BASE_URL",
  "OMNIROUTE_MODEL",
  "STUDIO_ACCESS_TOKEN",
  RATE_LIMIT_ENV,
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_ALLOWED_GROUPS",
];

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";
  resetRateLimits();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
  resetRateLimits();
});

function post(payload: string, headers: Record<string, string> = {}): Promise<Response> {
  return POST(
    new Request("http://studio.test/api/generate", {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: payload,
    }),
  );
}

function stubFetch(impl: (url: string, init: RequestInit) => Promise<Response>) {
  const mock = vi.fn(impl);
  vi.stubGlobal("fetch", mock);
  return mock;
}

function sseResponse(frames: string[]): Response {
  return new Response(frames.join(""), {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const OK_STREAM = ['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"];

describe("rate limit configuration", () => {
  it("falls back to the default when unset", () => {
    expect(readRateLimitConfig()).toBe(DEFAULT_RATE_LIMIT_PER_MIN);
  });

  it("accepts a configured limit", () => {
    process.env[RATE_LIMIT_ENV] = "7";
    expect(readRateLimitConfig()).toBe(7);
  });

  it("ignores junk and caps an absurd value", () => {
    process.env[RATE_LIMIT_ENV] = "not-a-number";
    expect(readRateLimitConfig()).toBe(DEFAULT_RATE_LIMIT_PER_MIN);
    process.env[RATE_LIMIT_ENV] = "999999";
    expect(readRateLimitConfig()).toBe(MAX_RATE_LIMIT_PER_MIN);
  });
});

describe("rate limit accounting", () => {
  it("allows up to the limit and then refuses with retry-after", () => {
    process.env[RATE_LIMIT_ENV] = "3";
    // A distinct identity per test keeps the shared module state predictable.
    const identity = `acct-${Math.random()}`;

    expect(checkRateLimit(identity).ok).toBe(true);
    expect(checkRateLimit(identity).ok).toBe(true);
    const third = checkRateLimit(identity);
    expect(third.ok).toBe(true);
    expect(third.remaining).toBe(0);

    const fourth = checkRateLimit(identity);
    expect(fourth.ok).toBe(false);
    expect(fourth.retryAfterSeconds).toBeGreaterThan(0);
    expect(fourth.retryAfterSeconds).toBeLessThanOrEqual(60);
  });

  it("opens a fresh window when the minute rolls over", () => {
    const identity = `acct-${Math.random()}`;
    const minute = 1_700_000_000_000;
    const inWindow = (ms: number) => minute + ms;

    process.env[RATE_LIMIT_ENV] = "1";
    expect(checkRateLimit(identity, inWindow(0)).ok).toBe(true);
    expect(checkRateLimit(identity, inWindow(30_000)).ok).toBe(false);
    // Next fixed window: the counter starts over.
    expect(checkRateLimit(identity, inWindow(60_001)).ok).toBe(true);
  });

  it("keeps identities isolated from each other", () => {
    process.env[RATE_LIMIT_ENV] = "1";
    expect(checkRateLimit("identity-a").ok).toBe(true);
    expect(checkRateLimit("identity-a").ok).toBe(false);
    expect(checkRateLimit("identity-b").ok).toBe(true);
  });
});

describe("rate limit enforcement in the route", () => {
  it("answers 429 with retry-after once the identity exceeds its budget", async () => {
    process.env[RATE_LIMIT_ENV] = "1";
    stubFetch(async () => sseResponse(OK_STREAM));

    // Token callers are keyed by the token itself, so set one and present it.
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    const headers = { "x-studio-token": "shared-secret" };
    const first = await post(JSON.stringify({ prompt: "a counter" }), headers);
    expect(first.status).toBe(200);

    const second = await post(JSON.stringify({ prompt: "a counter" }), headers);
    expect(second.status).toBe(429);
    expect(Number(second.headers.get("retry-after"))).toBeGreaterThan(0);
    expect((await second.json()).error).toMatch(/too many/i);
  });

  it("counts only requests that pass every earlier gate", async () => {
    process.env[RATE_LIMIT_ENV] = "1";
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    stubFetch(async () => sseResponse(OK_STREAM));
    const headers = { "x-studio-token": "shared-secret" };

    // A rejected request must not consume the budget: with a limit of 1, an
    // invalid attempt followed by a real generation still succeeds…
    const invalid = await post(JSON.stringify({ prompt: "   " }), headers);
    expect(invalid.status).toBe(400);
    const ok = await post(JSON.stringify({ prompt: "a counter" }), headers);
    expect(ok.status).toBe(200);

    // …and only then is the next attempt refused.
    const throttled = await post(JSON.stringify({ prompt: "a counter" }), headers);
    expect(throttled.status).toBe(429);
  });

  it("lets a different token keep working when one is throttled", async () => {
    process.env[RATE_LIMIT_ENV] = "1";
    stubFetch(async () => sseResponse(OK_STREAM));

    // First operator burns their budget through their own token…
    process.env.STUDIO_ACCESS_TOKEN = "operator-a";
    const a = { "x-studio-token": "operator-a" };
    expect((await post(JSON.stringify({ prompt: "a" }), a)).status).toBe(200);
    expect((await post(JSON.stringify({ prompt: "a" }), a)).status).toBe(429);

    // …and a second operator still generates.
    process.env.STUDIO_ACCESS_TOKEN = "operator-b";
    const response = await post(JSON.stringify({ prompt: "a" }), { "x-studio-token": "operator-b" });
    expect(response.status).toBe(200);
  });
});
