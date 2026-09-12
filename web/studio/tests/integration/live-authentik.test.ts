import { beforeAll, describe, expect, it } from "vitest";

/**
 * Drives the real authorization-code + PKCE handshake against a LIVE Authentik.
 *
 * This is the one thing the unit suite cannot cover: everything in
 * `tests/auth.test.ts` runs against `tests/helpers/mock-oidc.ts`, so the
 * provider's own behaviour — grant types, the signing key it actually picks,
 * which claims it really releases — is assumed there and checked here.
 *
 * Opt-in by design. Every variable below must be set or the whole file skips,
 * so `npm test` stays hermetic and offline. Run it deliberately:
 *
 *   STUDIO_E2E_BASE_URL=http://localhost:3001 \
 *   STUDIO_E2E_AUTHENTIK_URL=https://auth.cerulean.innotel.us \
 *   STUDIO_E2E_USERNAME=akadmin \
 *   STUDIO_E2E_PASSWORD=... \
 *   npx vitest run tests/integration
 *
 * Add STUDIO_E2E_INSECURE=1 for a lab provider with a self-signed certificate,
 * and STUDIO_E2E_GENERATE=1 to also prove the model gateway round-trip. Set
 * STUDIO_E2E_EXPECT_DENIED=1 when Studio's OIDC_ALLOWED_GROUPS excludes this
 * account, and the same run asserts the 403 lockdown instead of a session.
 *
 * The Studio instance under test must have its redirect URI registered on the
 * provider. Note it performs a real sign-in, so the account's last-login moves.
 */

const BASE = (process.env.STUDIO_E2E_BASE_URL ?? "").replace(/\/+$/, "");
const AUTH = (process.env.STUDIO_E2E_AUTHENTIK_URL ?? "").replace(/\/+$/, "");
const USERNAME = process.env.STUDIO_E2E_USERNAME ?? "";
const PASSWORD = process.env.STUDIO_E2E_PASSWORD ?? "";
const FLOW_SLUG = process.env.STUDIO_E2E_FLOW ?? "default-authentication-flow";
const RUN_GENERATE = process.env.STUDIO_E2E_GENERATE === "1";
/**
 * Set when Studio under test has OIDC_ALLOWED_GROUPS pointing at a group the
 * account below is NOT in. The same run then asserts the denial instead, so the
 * lockdown path is exercised against the real provider rather than assumed.
 */
const EXPECT_DENIED = process.env.STUDIO_E2E_EXPECT_DENIED === "1";

const required: Array<[string, string]> = [
  ["STUDIO_E2E_BASE_URL", BASE],
  ["STUDIO_E2E_AUTHENTIK_URL", AUTH],
  ["STUDIO_E2E_USERNAME", USERNAME],
  ["STUDIO_E2E_PASSWORD", PASSWORD],
];
const missing = required.filter(([, value]) => !value).map(([name]) => name);

if (process.env.STUDIO_E2E_INSECURE === "1") {
  // Lab Authentik deployments commonly present a self-signed certificate.
  process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
}

const MAX_BODY = 300;

/** A per-host cookie jar — Studio's flow cookie and the provider's session
 * cookie are scoped separately so neither leaks into the other's requests. */
class Jar {
  private readonly cookies = new Map<string, string>();

  absorb(response: Response): void {
    for (const raw of response.headers.getSetCookie()) {
      const [pair] = raw.split(";");
      const separator = pair.indexOf("=");
      if (separator === -1) continue;

      const name = pair.slice(0, separator).trim();
      const value = pair.slice(separator + 1).trim();

      // An empty value is how the callback clears the flow cookie.
      if (value) this.cookies.set(name, value);
      else this.cookies.delete(name);
    }
  }

  header(): string {
    return [...this.cookies].map(([name, value]) => `${name}=${value}`).join("; ");
  }

  get(name: string): string | undefined {
    return this.cookies.get(name);
  }
}

async function request(
  url: string,
  jar: Jar | null,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers);
  if (jar) {
    const cookie = jar.header();
    if (cookie) headers.set("cookie", cookie);
  }

  const response = await fetch(url, { ...init, headers, redirect: "manual" });

  // undici has no cookie store, so every response has to be harvested by hand.
  // Missing this is invisible for the first hop and then fails as "the provider
  // keeps asking for identification" further into the flow.
  if (jar) jar.absorb(response);

  return response;
}

async function bounded(response: Response): Promise<string> {
  try {
    return (await response.text()).slice(0, MAX_BODY);
  } catch {
    return "";
  }
}

/**
 * Follow a flow-executor call to its JSON result.
 *
 * Authentik answers a stage POST with a 302 back to the same executor URL and
 * only returns the next challenge on the following GET, so redirects are walked
 * by hand: undici does not manage cookies, and the 302 itself can set a session
 * cookie that the next hop needs.
 */
async function settle(
  url: string,
  jar: Jar,
  init: RequestInit = {},
): Promise<{ response: Response; json: Record<string, unknown> }> {
  let target = url;
  let current = init;

  for (let hop = 0; hop < 4; hop += 1) {
    const response = await request(target, jar, current);
    const location = response.headers.get("location");

    if (response.status >= 300 && response.status < 400 && location) {
      target = new URL(location, target).href;
      current = {}; // a redirect is always re-fetched as a GET
      continue;
    }

    const text = await response.text();
    if (!text) throw new Error(`empty response from ${target} (HTTP ${response.status})`);

    return { response, json: JSON.parse(text) as Record<string, unknown> };
  }

  throw new Error("too many redirects from the flow executor");
}

type FlowResult = {
  loginResponse: Response;
  authorizeUrl: string;
  callbackUrl: string;
  flowCookie: string;
  /** The signed session cookie Studio issued, harvested as an opaque string. */
  sessionHeader: string;
  callbackStatus: number;
  callbackBody: string;
  appStatus: number;
};

let flow: FlowResult;

/** Walk authorization-code + PKCE against the live provider, exactly as a
 * browser would, using Authentik's own flow-executor API for the login stages. */
async function runFlow(): Promise<FlowResult> {
  const studio = new Jar();
  const provider = new Jar();

  // 1. Studio mints state/nonce/PKCE and redirects to the provider.
  const loginResponse = await request(`${BASE}/api/auth/login`, studio);
  const authorizeUrl = loginResponse.headers.get("location") ?? "";
  if (!authorizeUrl) throw new Error(`/api/auth/login did not redirect (${loginResponse.status})`);

  // 2. The provider routes an unauthenticated request into its login flow.
  const authorizeHop = await request(authorizeUrl, provider);
  const flowLocation = authorizeHop.headers.get("location") ?? "";

  const flowUrl = new URL(flowLocation, `${AUTH}/`);
  if (!flowUrl.href.includes(FLOW_SLUG)) {
    throw new Error(
      `expected the login flow, got ${flowLocation || `HTTP ${authorizeHop.status}`} — ` +
        `is auth enabled on ${BASE}, and is the provider reachable?`,
    );
  }

  // 3. Drive the login flow through the API the Authentik SPA itself uses.
  const executor = `${AUTH}/api/v3/flows/executor/${FLOW_SLUG}/?${new URLSearchParams({ query: flowUrl.search }).toString()}`;

  let { json: challenge } = await settle(executor, provider);
  let component = String(challenge.component ?? "");

  // The identification stage answers under `uid_field`, not the configured
  // user_fields names — it is the identity field, whichever one that is.
  const answer = async (body: Record<string, unknown>) => {
    ({ json: challenge } = await settle(executor, provider, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(body),
    }));
    component = String(challenge.component ?? "");
  };

  for (let step = 0; step < 6; step += 1) {
    if (component === "ak-stage-identification") await answer({ component, uid_field: USERNAME });
    else if (component === "ak-stage-password") await answer({ component, password: PASSWORD });
    else if (component === "xak-flow-redirect") break;
    else throw new Error(`unexpected flow stage "${component}": ${JSON.stringify(challenge).slice(0, 160)}`);
  }

  if (component !== "xak-flow-redirect") {
    throw new Error(`login did not complete; last stage was "${component}"`);
  }

  // 4. Back through /authorize, which mints the code, then to our callback.
  let target = new URL(String(challenge.to), `${AUTH}/`).href;
  let callbackUrl = "";
  for (let hop = 0; hop < 3; hop += 1) {
    if (target.includes("code=")) {
      callbackUrl = target;
      break;
    }
    const response = await request(target, provider);
    const location = response.headers.get("location");
    if (!location) break;
    target = new URL(location, target).href;
  }
  if (!callbackUrl) throw new Error("the provider never redirected back with an authorization code");

  // 5. Studio exchanges the code and (if the policy allows) issues a session.
  const flowCookie = studio.get("studio_oidc_flow") ?? "";
  const callbackResponse = await request(callbackUrl, studio);
  const callbackBody = await bounded(callbackResponse);

  const sessionHeader = callbackResponse.headers
    .getSetCookie()
    .find((cookie) => cookie.startsWith("studio_session=") && cookie !== "studio_session=;")
    ?.split(";")[0] ?? "";

  // 6. The session actually unlocks the app.
  const appResponse = await request(`${BASE}/`, studio);

  return {
    loginResponse,
    authorizeUrl,
    callbackUrl,
    flowCookie,
    sessionHeader,
    callbackStatus: callbackResponse.status,
    callbackBody,
    appStatus: appResponse.status,
  };
}

describe.skipIf(missing.length > 0)("live provider integration", () => {
  beforeAll(async () => {
    flow = await runFlow();
  }, 60_000);

  it("sends the browser to the provider with PKCE S256", () => {
    expect(flow.loginResponse.status).toBe(307);

    const url = new URL(flow.authorizeUrl);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBeTruthy();
    expect(url.searchParams.get("state")).toBeTruthy();
    expect(url.searchParams.get("nonce")).toBeTruthy();
  });

  it("applies the group policy to the signed-in account", () => {
    if (EXPECT_DENIED) {
      // Authentication succeeded, authorization did not: 403, and crucially no
      // session is handed out for an account outside OIDC_ALLOWED_GROUPS.
      expect(flow.callbackStatus, flow.callbackBody).toBe(403);
      expect(flow.callbackBody).toMatch(/not in a group allowed/i);
      expect(flow.sessionHeader).toBe("");
      return;
    }

    expect(flow.sessionHeader).toBeTruthy();
    expect(flow.appStatus).toBe(200);
  });

  it("refuses a replayed authorization code", async () => {
    const response = await request(flow.callbackUrl, null, {
      headers: { cookie: `studio_oidc_flow=${flow.flowCookie}` },
    });

    expect(response.status).toBe(401);
    expect(await bounded(response)).toMatch(/sign-in failed|invalid_grant/i);
  });

  it("refuses a tampered state", async () => {
    const tampered = flow.callbackUrl.replace(/state=[^&]+/, "state=not-the-real-state");
    const response = await request(tampered, null, {
      headers: { cookie: `studio_oidc_flow=${flow.flowCookie}` },
    });

    expect(response.status).toBe(401);
    expect(await bounded(response)).toMatch(/state mismatch/i);
  });

  it("refuses a callback with no flow cookie", async () => {
    const response = await request(flow.callbackUrl, null);
    expect(response.status).toBe(401);
  });

  it("protects the generate route from anonymous callers", async () => {
    const response = await request(`${BASE}/api/generate`, null, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prompt: "a counter" }),
    });

    expect(response.status).toBe(401);
  });

  it.skipIf(!RUN_GENERATE || EXPECT_DENIED)("streams a generated app through the real gateway", async () => {
    const session = flow.sessionHeader.split("=").slice(1).join("=");
    const response = await request(`${BASE}/api/generate`, null, {
      method: "POST",
      headers: { "content-type": "application/json", cookie: `studio_session=${session}` },
      body: JSON.stringify({ prompt: "Write one file, index.html: a page that shows the word hi." }),
    });

    const body = await response.text();
    expect(response.status, body.slice(0, MAX_BODY)).toBe(200);
    expect(body).toMatch(/<file path=/);
  }, 180_000);
});

describe("live provider integration (configuration)", () => {
  it("skips cleanly when the opt-in variables are absent", () => {
    if (missing.length === 0) return;
    // Loud enough to be noticed, quiet enough not to fail an offline run.
    console.info(
      `live provider integration skipped — set ${missing.join(", ")} to enable it`,
    );
    expect(missing.length).toBeGreaterThan(0);
  });
});
