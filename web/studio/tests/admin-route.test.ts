import { mkdirSync, rmSync } from "node:fs";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/admin/status/route";
import AdminPanel from "@/components/AdminPanel";
import { SESSION_COOKIE, readAuthConfig, signSession, type Session } from "@/lib/auth";
import { resetModelCache } from "@/lib/omniroute";

// See page.test.ts: the request headers and the redirect are both stubbed with the
// contracts Next uses, and the repo .env loader is neutralized so a checkout with
// real credentials cannot decide an assertion.
const requestHeaders = { current: new Headers() };
const redirects: string[] = [];

vi.mock("next/headers", () => ({
  headers: async () => requestHeaders.current,
}));

vi.mock("next/navigation", () => ({
  redirect: (url: string): never => {
    redirects.push(url);
    throw new Error(`NEXT_REDIRECT:${url}`);
  },
}));

vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {}, repoRoot: () => process.cwd() }));

const ROOT = join(process.cwd(), "tests", ".tmp", "admin-route");

const MANAGED = [
  "OMNIROUTE_API_KEY",
  "OMNIROUTE_BASE_URL",
  "OMNIROUTE_MODEL",
  "STUDIO_ACCESS_TOKEN",
  "STUDIO_BUILD_QUEUE_DIR",
  "STUDIO_BUILDS_DIR",
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_SCOPES",
  "OIDC_ALLOWED_GROUPS",
  "OLYMPUS_ADMIN_GROUPS",
  "STUDIO_SESSION_SECRET",
] as const;

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }

  rmSync(ROOT, { recursive: true, force: true });
  mkdirSync(join(ROOT, "build-queue"), { recursive: true });
  mkdirSync(join(ROOT, "builds"), { recursive: true });

  process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";
  process.env.OMNIROUTE_BASE_URL = "http://192.168.1.46:20129/v1";
  process.env.STUDIO_BUILD_QUEUE_DIR = join(ROOT, "build-queue");
  process.env.STUDIO_BUILDS_DIR = join(ROOT, "builds");

  // The panel probes the gateway; a test must not depend on one existing.
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ data: [{ id: "auto/coding", owned_by: "combo" }] })));
  resetModelCache();
  redirects.length = 0;
  requestHeaders.current = new Headers();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  rmSync(ROOT, { recursive: true, force: true });
  vi.unstubAllGlobals();
  resetModelCache();
});

const OIDC = {
  OIDC_ISSUER_URL: "https://auth.cerulean.innotel.us/application/o/studio/",
  OIDC_CLIENT_ID: "studio",
  OIDC_CLIENT_SECRET: "not-a-placeholder",
  STUDIO_SESSION_SECRET: "test-session-secret",
} as const;

function configure(extra: Record<string, string> = {}): void {
  for (const [key, value] of Object.entries({ ...OIDC, ...extra })) {
    process.env[key] = value;
  }
}

function cookieFor(session: Partial<Session> & { sub: string }): string {
  const config = readAuthConfig();
  if (!config) throw new Error("test setup: OIDC is not configured");
  return `${SESSION_COOKIE}=${signSession(session, config)}`;
}

function get(headers: Record<string, string> = {}): Promise<Response> {
  return GET(new Request("http://studio.test/api/admin/status", { headers }));
}

describe("the status route's gate", () => {
  it("sends an unauthenticated caller to sign in rather than forbidding them", async () => {
    configure();
    expect((await get()).status).toBe(401);
  });

  it("refuses an authenticated caller outside the admin group", async () => {
    configure({ OLYMPUS_ADMIN_GROUPS: "authentik Admins" });

    const response = await get({ cookie: cookieFor({ sub: "user-1", groups: ["olympus-all"] }) });

    expect(response.status).toBe(403);
    // Signing in again will not help, and the message says what will.
    expect((await response.json()).error).toMatch(/not in a group allowed to administer/i);
  });

  it("lets a caller in the admin group read it", async () => {
    configure({ OLYMPUS_ADMIN_GROUPS: "authentik Admins" });

    const response = await get({ cookie: cookieFor({ sub: "user-1", groups: ["authentik Admins"] }) });

    expect(response.status).toBe(200);
    expect((await response.json()).checks).toBeInstanceOf(Array);
  });

  it("fails closed for a session that carries no groups at all", async () => {
    configure({ OLYMPUS_ADMIN_GROUPS: "authentik Admins" });

    expect((await get({ cookie: cookieFor({ sub: "user-1" }) })).status).toBe(403);
  });

  it("keeps the allow-list and the admin list separate", async () => {
    // An operator may reasonably let everyone build and only some administer.
    configure({ OIDC_ALLOWED_GROUPS: "olympus-all", OLYMPUS_ADMIN_GROUPS: "authentik Admins" });

    const builder = await get({ cookie: cookieFor({ sub: "user-1", groups: ["olympus-all"] }) });

    expect(builder.status).toBe(403);
  });

  it("requires the shared access token when one is configured", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    process.env.OLYMPUS_ADMIN_GROUPS = "";

    expect((await get()).status).toBe(401);
    expect((await get({ "x-studio-token": "shared-secret" })).status).toBe(200);
  });

  it("is open on a single-operator deployment with no identity provider", async () => {
    expect((await get()).status).toBe(200);
  });

  it("never lets a shared cache hold deployment state", async () => {
    const response = await get();
    expect(response.headers.get("cache-control")).toBe("no-store");
  });
});

describe("the panel page is a shell, not a door", () => {
  it("sends a visitor with no session to the provider", async () => {
    configure();
    requestHeaders.current = new Headers({ "x-forwarded-host": "studio.olympus.innotel.us" });

    const Page = (await import("@/app/admin/page")).default;

    await expect(Page()).rejects.toThrow("NEXT_REDIRECT:/api/auth/login");
    expect(redirects).toEqual(["/api/auth/login"]);
  });

  it("explains a group refusal instead of looping through the provider", async () => {
    configure({ OLYMPUS_ADMIN_GROUPS: "authentik Admins" });
    requestHeaders.current = new Headers({
      "x-forwarded-host": "studio.olympus.innotel.us",
      cookie: cookieFor({ sub: "user-1", email: "builder@example.com" }),
    });

    const Page = (await import("@/app/admin/page")).default;
    const element = (await Page()) as { type?: unknown; props?: { children?: unknown } };

    expect(redirects).toEqual([]);
    expect(element.type).toBe("main");
    expect(JSON.stringify(element)).toContain("Not your deployment to administer");
  });

  it("hands an admin the panel", async () => {
    configure();
    requestHeaders.current = new Headers({
      cookie: cookieFor({ sub: "user-1", email: "operator@example.com", groups: [] }),
    });

    const Page = (await import("@/app/admin/page")).default;
    const element = (await Page()) as { type?: unknown; props?: { viewer?: unknown } };

    expect(redirects).toEqual([]);
    // Identified by reference: a function's name is not in its JSON.
    expect(element.type).toBe(AdminPanel);
    expect(element.props?.viewer).toBe("operator@example.com");
  });
});
