import { afterAll, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The host split: two public names, two roles, one app.
 *
 *   studio.olympus.innotel.us  — the tool. A visitor without a session is sent
 *                                to the provider; there is nothing to look at.
 *   olympus.innotel.us         — the front door. A visitor without a session
 *                                gets a landing screen with one way in.
 *
 * Both hosts are registered as redirect URIs, so this is purely an
 * entry-experience decision — and it is the one that regressed before (the
 * root host served the builder and bounced to sign-in with no explanation),
 * so it is pinned here rather than left to the live check.
 */

// The page reads the incoming request's headers and ends a redirect by
// throwing, exactly as Next does. Both helpers are stubbed with those
// contracts: a mutable Headers for the request, and a `redirect` that records
// the target and throws.
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

// Neutralize the repo-.env loader: `delete process.env[key]` is not isolation,
// because loadRepoEnv() reads the key straight back off disk. Without this the
// suite's behavior would depend on whatever credentials the checkout carries.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

import Page from "@/app/page";
import Studio from "@/components/Studio";
import { SESSION_COOKIE, readAuthConfig, signSession } from "@/lib/auth";

const STUDIO_HOST = "studio.olympus.innotel.us";
const ROOT_HOST = "olympus.innotel.us";

const MANAGED = [
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "STUDIO_SESSION_SECRET",
  "OIDC_ALLOWED_GROUPS",
] as const;

const saved: Record<string, string | undefined> = {};

/** Every string in a React element tree, so assertions can be about copy. */
function textOf(node: unknown): string {
  if (node === null || node === undefined || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join(" ");
  if (typeof node === "object" && "props" in node) {
    return textOf((node as { props?: { children?: unknown } }).props?.children);
  }
  return "";
}

/** Every `href` in a React element tree. */
function hrefsOf(node: unknown): string[] {
  if (node === null || node === undefined || typeof node !== "object") return [];
  if (Array.isArray(node)) return node.flatMap(hrefsOf);
  if (!("props" in node)) return [];
  const props = (node as { props?: Record<string, unknown> }).props ?? {};
  const own = typeof props.href === "string" ? [props.href] : [];
  return [...own, ...hrefsOf(props.children)];
}

function configureOidc(): void {
  process.env.OIDC_ISSUER_URL = "https://auth.cerulean.innotel.us/application/o/studio/";
  process.env.OIDC_CLIENT_ID = "studio";
  process.env.OIDC_CLIENT_SECRET = "test-client-secret";
  process.env.STUDIO_SESSION_SECRET = "test-session-secret";
  process.env.OIDC_REDIRECT_URI = "";
  process.env.OIDC_ALLOWED_GROUPS = "";
}

function headerFor(host: string, session?: string): void {
  const headers = new Headers();
  headers.set("x-forwarded-host", host);
  headers.set("x-forwarded-proto", "https");
  if (session) headers.set("cookie", `${SESSION_COOKIE}=${session}`);
  requestHeaders.current = headers;
}

function sessionCookie(): string {
  const config = readAuthConfig();
  if (!config) throw new Error("test setup: OIDC is not configured");
  return signSession({ sub: "user-1", email: "operator@example.com", name: "Operator" }, config);
}

beforeEach(() => {
  for (const key of MANAGED) {
    if (!(key in saved)) saved[key] = process.env[key];
    delete process.env[key];
  }
  redirects.length = 0;
  requestHeaders.current = new Headers();
});

afterAll(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

describe("the root host is the front door", () => {
  it("shows a landing screen — not a redirect — to a visitor with no session", async () => {
    configureOidc();
    headerFor(ROOT_HOST);

    const element = await Page();

    expect(redirects).toEqual([]);
    expect((element as { type?: unknown }).type).toBe("main");

    const text = textOf(element);
    expect(text).toContain("Describe an app");
    // Exactly one way in, and it starts sign-in on this host.
    expect(hrefsOf(element)).toContain("/api/auth/login");
  });

  it("describes the product rather than showing a builder it cannot use", async () => {
    configureOidc();
    headerFor(ROOT_HOST);

    const text = textOf(await Page());

    expect(text).toContain("Sign in to start building");
    expect(text).toContain("Olympus");
  });

  it("hands a signed-in visitor the builder", async () => {
    configureOidc();
    headerFor(ROOT_HOST, sessionCookie());

    const element = await Page();

    expect(redirects).toEqual([]);
    expect((element as { type?: unknown }).type).toBe(Studio);
    expect((element as { props?: { user?: unknown } }).props?.user).toBe("Operator");
  });
});

describe("the studio host is the tool", () => {
  it("sends a visitor with no session straight to the provider", async () => {
    configureOidc();
    headerFor(STUDIO_HOST);

    await expect(Page()).rejects.toThrow("NEXT_REDIRECT:/api/auth/login");
    expect(redirects).toEqual(["/api/auth/login"]);
  });

  it("hands a signed-in visitor the builder", async () => {
    configureOidc();
    headerFor(STUDIO_HOST, sessionCookie());

    const element = await Page();

    expect(redirects).toEqual([]);
    expect((element as { type?: unknown }).type).toBe(Studio);
  });

  it("does not treat the root host as the studio, even when both are forwarded", async () => {
    configureOidc();
    headerFor(`${STUDIO_HOST}, ${ROOT_HOST}`);

    await expect(Page()).rejects.toThrow("NEXT_REDIRECT:/api/auth/login");
  });
});

describe("when OIDC is not configured", () => {
  it("shows the builder on both hosts — there is no identity to gate on", async () => {
    headerFor(ROOT_HOST);
    expect((await Page() as { type?: unknown }).type).toBe(Studio);

    headerFor(STUDIO_HOST);
    expect((await Page() as { type?: unknown }).type).toBe(Studio);

    expect(redirects).toEqual([]);
  });

  it("treats a leftover placeholder client secret as unconfigured", async () => {
    configureOidc();
    process.env.OIDC_CLIENT_SECRET = "change-me";
    headerFor(STUDIO_HOST);

    expect((await Page() as { type?: unknown }).type).toBe(Studio);
    expect(redirects).toEqual([]);
  });
});
