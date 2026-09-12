import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { GET as callbackGet } from "@/app/api/auth/callback/route";
import { GET as loginGet } from "@/app/api/auth/login/route";
import { POST as generatePost } from "@/app/api/generate/route";
import {
  FLOW_COOKIE,
  SESSION_COOKIE,
  buildAuthorizeUrl,
  clearCookie,
  createPkce,
  discover,
  exchangeCode,
  isAuthorized,
  isSecureRequestOrigin,
  normalizeGroups,
  parseGroupList,
  readAuthConfig,
  readCookie,
  readFlow,
  readSession,
  resolveRedirectUri,
  serializeCookie,
  sessionFromClaims,
  signFlow,
  signSession,
  verifyIdToken,
} from "@/lib/auth";
import { startMockOidc, type MockOidc } from "./helpers/mock-oidc";

// Neutralize the repo-.env loader: `delete process.env[key]` is not isolation,
// because loadRepoEnv() reads the key straight back off disk. Without this the
// suite's behavior depends on whatever credentials the checkout happens to
// carry. (env.test.ts exercises the loader itself against temp directories.)
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

const CLIENT_ID = "studio";
const CLIENT_SECRET = "test-client-secret";
const MANAGED = [
  "OIDC_ISSUER_URL",
  "OIDC_CLIENT_ID",
  "OIDC_CLIENT_SECRET",
  "OIDC_REDIRECT_URI",
  "OIDC_SCOPES",
  "OIDC_ALLOWED_GROUPS",
  "STUDIO_SESSION_SECRET",
  "OMNIROUTE_API_KEY",
];

let oidc: MockOidc;
let saved: Record<string, string | undefined> = {};

beforeAll(async () => {
  oidc = await startMockOidc({ clientId: CLIENT_ID });
});

afterAll(async () => {
  await oidc.close();
});

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }

  process.env.OIDC_ISSUER_URL = oidc.issuer;
  process.env.OIDC_CLIENT_ID = CLIENT_ID;
  process.env.OIDC_CLIENT_SECRET = CLIENT_SECRET;

  oidc.setNonce("nonce-default");
  oidc.setClaims({});
  oidc.resetSigning();
  oidc.failTokenEndpoint(0);
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
});

function cookieValue(headers: Headers, name: string): string | undefined {
  for (const raw of headers.getSetCookie()) {
    const pair = raw.split(";")[0];
    const separator = pair.indexOf("=");
    if (separator === -1) continue;
    if (pair.slice(0, separator).trim() === name) return pair.slice(separator + 1);
  }
  return undefined;
}

async function mintIdToken(): Promise<string> {
  const config = readAuthConfig()!;
  const tokens = await exchangeCode(config, "code", "verifier", "http://studio.test/api/auth/callback");
  if (!tokens.id_token) throw new Error("mock returned no id_token");
  return tokens.id_token;
}

async function completeFlow() {
  const loginResponse = await loginGet(new Request("http://studio.test/api/auth/login"));
  const authorizeUrl = new URL(loginResponse.headers.get("location")!);
  const flowCookie = cookieValue(loginResponse.headers, FLOW_COOKIE)!;

  const config = readAuthConfig()!;
  const flow = readFlow(`${FLOW_COOKIE}=${flowCookie}`, config)!;
  const state = authorizeUrl.searchParams.get("state")!;

  oidc.setNonce(flow.nonce);

  const callbackResponse = await callbackGet(
    new Request(`http://studio.test/api/auth/callback?code=auth-code&state=${state}`, {
      headers: { cookie: `${FLOW_COOKIE}=${flowCookie}` },
    }),
  );

  return {
    loginResponse,
    authorizeUrl,
    flowCookie,
    flow,
    state,
    callbackResponse,
    sessionCookie: cookieValue(callbackResponse.headers, SESSION_COOKIE),
  };
}

/* ---- configuration ------------------------------------------------------ */

describe("readAuthConfig", () => {
  it("is disabled when the issuer is empty", () => {
    process.env.OIDC_ISSUER_URL = "";
    expect(readAuthConfig()).toBeNull();
  });

  it("is disabled while the client secret is still the template placeholder", () => {
    process.env.OIDC_CLIENT_SECRET = "change-me-oidc-client-secret";
    expect(readAuthConfig()).toBeNull();
  });

  it("derives a session secret from the client secret when none is set", () => {
    const config = readAuthConfig()!;
    expect(config.sessionSecret).toMatch(/^[0-9a-f]{64}$/);

    process.env.STUDIO_SESSION_SECRET = "explicit-secret";
    expect(readAuthConfig()!.sessionSecret).toBe("explicit-secret");
  });

  it("defaults the scope to openid email profile", () => {
    expect(readAuthConfig()!.scopes).toBe("openid email profile");
  });
});

describe("discovery", () => {
  it("resolves the provider endpoints", async () => {
    const config = readAuthConfig()!;
    const document = await discover(config);

    // The endpoints live on the origin; the issuer carries the application path.
    const origin = new URL(oidc.issuer).origin;
    expect(document.issuer).toBe(oidc.issuer);
    expect(document.authorization_endpoint).toBe(`${origin}/application/o/authorize/`);
    expect(document.token_endpoint).toBe(`${origin}/application/o/token/`);
    expect(document.jwks_uri).toBe(`${origin}/application/o/studio/jwks/`);
  });

  it("caches the discovery document", async () => {
    const config = readAuthConfig()!;
    const before = oidc.counts().discovery;
    await discover(config);
    await discover(config);
    expect(oidc.counts().discovery).toBe(before);
  });
});

describe("redirect URI resolution", () => {
  it("derives the callback from the request when unset", () => {
    const config = readAuthConfig()!;
    const request = new Request("http://studio.test/api/auth/login");
    expect(resolveRedirectUri(config, request)).toBe("http://studio.test/api/auth/callback");
  });

  it("honors the edge's forwarded proto and host", () => {
    const config = readAuthConfig()!;
    const request = new Request("http://internal:3001/api/auth/login", {
      headers: { "x-forwarded-proto": "https", "x-forwarded-host": "studio.innotel.us" },
    });
    expect(resolveRedirectUri(config, request)).toBe("https://studio.innotel.us/api/auth/callback");
    expect(isSecureRequestOrigin(request)).toBe(true);
  });

  it("prefers the configured redirect URI", () => {
    process.env.OIDC_REDIRECT_URI = "https://olympus.innotel.us/api/auth/callback";
    const config = readAuthConfig()!;
    expect(resolveRedirectUri(config, new Request("http://studio.test/api/auth/login"))).toBe(
      "https://olympus.innotel.us/api/auth/callback",
    );
  });
});

describe("authorize URL", () => {
  it("carries PKCE, state, nonce and scope", async () => {
    const config = readAuthConfig()!;
    const document = await discover(config);
    const { verifier, challenge } = createPkce();

    const url = new URL(
      buildAuthorizeUrl(config, document, {
        state: "state-1",
        nonce: "nonce-1",
        verifier,
        challenge,
        redirectUri: "http://studio.test/api/auth/callback",
      }),
    );

    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("client_id")).toBe(CLIENT_ID);
    expect(url.searchParams.get("code_challenge")).toBe(challenge);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("state")).toBe("state-1");
    expect(url.searchParams.get("nonce")).toBe("nonce-1");
    expect(url.searchParams.get("scope")).toContain("openid");
    expect(challenge).not.toBe(verifier);
  });
});

/* ---- id_token verification ---------------------------------------------- */

describe("verifyIdToken", () => {
  it("accepts a valid RS256 token", async () => {
    const config = readAuthConfig()!;
    const claims = await verifyIdToken(await mintIdToken(), config, "nonce-default");

    expect(claims.sub).toBe("operator-123");
    expect(claims.email).toBe("operator@example.com");
  });

  it("accepts a valid ES256 token (raw JWS signature conversion)", async () => {
    oidc.useEcKey();
    const config = readAuthConfig()!;
    const claims = await verifyIdToken(await mintIdToken(), config, "nonce-default");

    expect(claims.sub).toBe("operator-123");
  });

  it("rejects a token signed by a key that is not in the JWKS", async () => {
    oidc.useUnknownSigningKey();
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).rejects.toThrow(/JWKS entry/i);
  });

  it("rejects a tampered signature", async () => {
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    const parts = token.split(".");
    const signature = Buffer.from(parts[2], "base64url");
    signature[0] ^= 0xff;
    const tampered = `${parts[0]}.${parts[1]}.${signature.toString("base64url")}`;

    await expect(verifyIdToken(tampered, config, "nonce-default")).rejects.toThrow(/did not verify/i);
  });

  it("rejects the none algorithm", async () => {
    oidc.setAlgorithm("none");
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).rejects.toThrow(/Unsupported id_token algorithm/i);
  });

  it("rejects a wrong issuer", async () => {
    oidc.setClaims({ iss: "https://evil.example" });
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).rejects.toThrow(/issuer mismatch/i);
  });

  it("rejects a wrong audience", async () => {
    oidc.setClaims({ aud: "some-other-app" });
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).rejects.toThrow(/audience/i);
  });

  it("accepts an audience array that includes this client", async () => {
    oidc.setClaims({ aud: ["some-other-app", CLIENT_ID] });
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).resolves.toBeTruthy();
  });

  it("rejects an expired token", async () => {
    oidc.setClaims({ exp: Math.floor(Date.now() / 1000) - 600 });
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "nonce-default")).rejects.toThrow(/expired/i);
  });

  it("rejects a replayed nonce", async () => {
    const token = await mintIdToken();
    const config = readAuthConfig()!;

    await expect(verifyIdToken(token, config, "different-nonce")).rejects.toThrow(/nonce mismatch/i);
  });
});

/* ---- session cookies ---------------------------------------------------- */

describe("session cookies", () => {
  it("round-trips a session", () => {
    const config = readAuthConfig()!;
    const cookie = signSession({ sub: "user-1", email: "a@b.test" }, config);

    expect(readSession(`${SESSION_COOKIE}=${cookie}`, config)?.email).toBe("a@b.test");
  });

  it("rejects a session signed with a different secret", () => {
    const config = readAuthConfig()!;
    const other = { ...config, sessionSecret: "a-different-secret" };
    const cookie = signSession({ sub: "user-1" }, other);

    expect(readSession(`${SESSION_COOKIE}=${cookie}`, config)).toBeNull();
  });

  it("rejects a tampered payload", () => {
    const config = readAuthConfig()!;
    const cookie = signSession({ sub: "user-1" }, config);
    const [payload, signature] = cookie.split(".");

    const forged = Buffer.from(JSON.stringify({ sub: "admin", exp: 9999999999 })).toString("base64url");
    expect(readSession(`${SESSION_COOKIE}=${forged}.${signature}`, config)).toBeNull();
    expect(payload).not.toBe(forged);
  });

  it("rejects garbage and absent cookies", () => {
    const config = readAuthConfig()!;
    expect(readSession(null, config)).toBeNull();
    expect(readSession(`${SESSION_COOKIE}=`, config)).toBeNull();
    expect(readSession(`${SESSION_COOKIE}=not-a-token`, config)).toBeNull();
  });

  it("round-trips flow state and rejects tampering", () => {
    const config = readAuthConfig()!;
    const flow = { state: "s", nonce: "n", verifier: "v" };
    const cookie = signFlow(flow, config);

    expect(readFlow(`${FLOW_COOKIE}=${cookie}`, config)).toEqual(expect.objectContaining(flow));
    expect(readFlow(`${FLOW_COOKIE}=${cookie}x`, config)).toBeNull();
  });

  it("serializes and clears cookies with the hardening flags", () => {
    expect(serializeCookie("a", "b", { maxAge: 60, secure: true })).toBe(
      "a=b; Path=/; HttpOnly; SameSite=Lax; Max-Age=60; Secure",
    );
    expect(clearCookie("a")).toContain("Max-Age=0");
    expect(readCookie("a=b; c=d", "c")).toBe("d");
    expect(readCookie(null, "c")).toBeUndefined();
  });

  it("maps claims into a session", () => {
    expect(sessionFromClaims({ sub: "s", preferred_username: "darnel" }).name).toBe("darnel");
    expect(sessionFromClaims({}).sub).toBe("unknown");
  });
});

/* ---- the routes --------------------------------------------------------- */

describe("login route", () => {
  it("redirects to the provider with a signed flow cookie", async () => {
    const response = await loginGet(new Request("http://studio.test/api/auth/login"));
    expect(response.status).toBe(307);

    const location = new URL(response.headers.get("location")!);
    expect(location.pathname).toBe("/application/o/authorize/");
    expect(location.searchParams.get("code_challenge_method")).toBe("S256");
    expect(cookieValue(response.headers, FLOW_COOKIE)).toBeTruthy();
  });

  it("omits Secure on plain http and sets it behind TLS", async () => {
    const insecure = await loginGet(new Request("http://studio.test/api/auth/login"));
    expect(insecure.headers.getSetCookie().join(";")).not.toContain("Secure");

    const secure = await loginGet(
      new Request("http://studio.test/api/auth/login", { headers: { "x-forwarded-proto": "https" } }),
    );
    expect(secure.headers.getSetCookie().join(";")).toContain("Secure");
  });

  it("is a no-op when auth is disabled", async () => {
    process.env.OIDC_ISSUER_URL = "";
    const response = await loginGet(new Request("http://studio.test/api/auth/login"));

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe("/");
  });

  it("reports an unreachable provider as 502", async () => {
    process.env.OIDC_ISSUER_URL = "http://127.0.0.1:1";
    const response = await loginGet(new Request("http://studio.test/api/auth/login"));

    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/identity provider/i);
  });
});

describe("callback route", () => {
  it("completes the authorization-code flow and issues a session", async () => {
    const { loginResponse, authorizeUrl, flowCookie, flow, state, callbackResponse, sessionCookie } =
      await completeFlow();

    expect(loginResponse.status).toBe(307);
    expect(authorizeUrl.searchParams.get("client_id")).toBe(CLIENT_ID);

    expect(callbackResponse.status).toBe(307);
    expect(callbackResponse.headers.get("location")).toBe("/");

    const config = readAuthConfig()!;
    const session = readSession(`${SESSION_COOKIE}=${sessionCookie}`, config);
    expect(session).toEqual(
      expect.objectContaining({ sub: "operator-123", email: "operator@example.com" }),
    );

    // The exchange must present our PKCE verifier and client authentication.
    const tokenRequest = oidc.lastTokenRequest()!;
    const params = new URLSearchParams(tokenRequest.body);
    expect(params.get("grant_type")).toBe("authorization_code");
    expect(params.get("code")).toBe("auth-code");
    expect(params.get("code_verifier")).toBe(flow.verifier);
    expect(params.get("redirect_uri")).toBe("http://studio.test/api/auth/callback");
    expect(tokenRequest.authorization).toBe(
      `Basic ${Buffer.from(`${CLIENT_ID}:${CLIENT_SECRET}`).toString("base64")}`,
    );

    // The flow cookie is single-use and cleared on success.
    expect(callbackResponse.headers.getSetCookie().join(";")).toContain(`${FLOW_COOKIE}=;`);
    expect(flowCookie).toBeTruthy();
  });

  it("rejects a mismatched state", async () => {
    const loginResponse = await loginGet(new Request("http://studio.test/api/auth/login"));
    const flowCookie = cookieValue(loginResponse.headers, FLOW_COOKIE)!;

    const response = await callbackGet(
      new Request("http://studio.test/api/auth/callback?code=x&state=wrong", {
        headers: { cookie: `${FLOW_COOKIE}=${flowCookie}` },
      }),
    );

    expect(response.status).toBe(401);
    expect(await response.text()).toMatch(/State mismatch/i);
  });

  it("rejects a callback with no flow cookie", async () => {
    const response = await callbackGet(
      new Request("http://studio.test/api/auth/callback?code=x&state=y"),
    );
    expect(response.status).toBe(401);
  });

  it("rejects a callback missing the code", async () => {
    const response = await callbackGet(new Request("http://studio.test/api/auth/callback?state=y"));
    expect(response.status).toBe(400);
  });

  it("surfaces a provider-side rejection", async () => {
    const response = await callbackGet(
      new Request("http://studio.test/api/auth/callback?error=access_denied&error_description=User%20said%20no"),
    );

    expect(response.status).toBe(401);
    expect(await response.text()).toMatch(/access_denied/);
  });

  it("rejects a replayed nonce against a fresh flow", async () => {
    const loginResponse = await loginGet(new Request("http://studio.test/api/auth/login"));
    const flowCookie = cookieValue(loginResponse.headers, FLOW_COOKIE)!;
    const state = new URL(loginResponse.headers.get("location")!).searchParams.get("state")!;

    // The mock keeps minting its default nonce, not the one this flow expects.
    oidc.setNonce("nonce-default");

    const response = await callbackGet(
      new Request(`http://studio.test/api/auth/callback?code=auth-code&state=${state}`, {
        headers: { cookie: `${FLOW_COOKIE}=${flowCookie}` },
      }),
    );

    expect(response.status).toBe(401);
    expect(await response.text()).toMatch(/nonce mismatch/i);
  });

  it("reports a failing token endpoint", async () => {
    oidc.failTokenEndpoint(400);

    const loginResponse = await loginGet(new Request("http://studio.test/api/auth/login"));
    const flowCookie = cookieValue(loginResponse.headers, FLOW_COOKIE)!;
    const state = new URL(loginResponse.headers.get("location")!).searchParams.get("state")!;
    const flow = readFlow(`${FLOW_COOKIE}=${flowCookie}`, readAuthConfig()!)!;
    oidc.setNonce(flow.nonce);

    const response = await callbackGet(
      new Request(`http://studio.test/api/auth/callback?code=auth-code&state=${state}`, {
        headers: { cookie: `${FLOW_COOKIE}=${flowCookie}` },
      }),
    );

    expect(response.status).toBe(401);
    expect(await response.text()).toMatch(/token endpoint returned 400/i);
  });
});

describe("route protection", () => {
  it("unlocks the generate route once signed in", async () => {
    const { sessionCookie } = await completeFlow();
    expect(sessionCookie).toBeTruthy();

    process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('data: {"choices":[{"delta":{"content":"x"}}]}\n\n', { status: 200, headers: { "content-type": "text/event-stream" } })),
    );

    const blocked = await generatePost(
      new Request("http://studio.test/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ prompt: "a counter" }),
      }),
    );
    expect(blocked.status).toBe(401);

    const allowed = await generatePost(
      new Request("http://studio.test/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json", cookie: `${SESSION_COOKIE}=${sessionCookie}` },
        body: JSON.stringify({ prompt: "a counter" }),
      }),
    );
    expect(allowed.status).toBe(200);
  });
});

describe("group policy", () => {
  // Authentik hands groups over as an array from `profile` and as a delimited
  // string from its dedicated `groups` mapping, so both shapes must parse.
  it("normalizes both claim shapes", () => {
    expect(normalizeGroups(["Cerulean", "Onyx"])).toEqual(["Cerulean", "Onyx"]);
    expect(normalizeGroups("Cerulean Onyx")).toEqual(["Cerulean", "Onyx"]);
    expect(normalizeGroups("Cerulean,Onyx")).toEqual(["Cerulean", "Onyx"]);
    expect(normalizeGroups("  Cerulean ")).toEqual(["Cerulean"]);
    expect(normalizeGroups(undefined)).toEqual([]);
    expect(normalizeGroups(42)).toEqual([]);
    expect(normalizeGroups(["ok", 7, null])).toEqual(["ok"]);
  });

  it("parses the allow-list, treating an empty value as unset", () => {
    expect(parseGroupList(undefined)).toEqual([]);
    expect(parseGroupList("")).toEqual([]);
    expect(parseGroupList(" Studio , Ops ")).toEqual(["Studio", "Ops"]);
  });

  it("keeps group names containing spaces intact", () => {
    // Authentik really does name groups like this; splitting on whitespace
    // would turn one group into two names that match nothing.
    expect(parseGroupList("authentik Agent-Users")).toEqual(["authentik Agent-Users"]);
    expect(parseGroupList("Cerulean,authentik Agent-Users")).toEqual([
      "Cerulean",
      "authentik Agent-Users",
    ]);
    expect(isAuthorized(
      { sub: "s", groups: ["authentik Agent-Users"] },
      { ...readAuthConfig()!, allowedGroups: parseGroupList("authentik Agent-Users") },
    )).toBe(true);
  });

  it("allows any authenticated user when the allow-list is empty", () => {
    const config = { ...readAuthConfig()!, allowedGroups: [] };
    expect(isAuthorized({ sub: "s" }, config)).toBe(true);
  });

  it("fails closed on a missing or non-matching groups claim", () => {
    const config = { ...readAuthConfig()!, allowedGroups: ["Studio"] };
    expect(isAuthorized({ sub: "s" }, config)).toBe(false);
    expect(isAuthorized({ sub: "s", groups: [] }, config)).toBe(false);
    expect(isAuthorized({ sub: "s", groups: ["Onyx"] }, config)).toBe(false);
    expect(isAuthorized({ sub: "s", groups: ["Onyx", "Studio"] }, config)).toBe(true);
  });

  it("matches group names exactly", () => {
    const config = { ...readAuthConfig()!, allowedGroups: ["Studio"] };
    expect(isAuthorized({ sub: "s", groups: ["studio"] }, config)).toBe(false);
  });

  it("issues a session when the token carries an allowed group", async () => {
    process.env.OIDC_ALLOWED_GROUPS = "Cerulean,Studio";
    oidc.setClaims({ groups: ["Onyx", "Studio"] });

    const { callbackResponse, sessionCookie } = await completeFlow();
    expect(callbackResponse.status).toBe(307);

    const config = readAuthConfig()!;
    expect(config.allowedGroups).toEqual(["Cerulean", "Studio"]);
    expect(readSession(`${SESSION_COOKIE}=${sessionCookie}`, config)?.groups).toEqual(["Onyx", "Studio"]);
  });

  it("refuses a signed-in user outside the allow-list with 403 and no session", async () => {
    process.env.OIDC_ALLOWED_GROUPS = "Studio";
    oidc.setClaims({ groups: ["Onyx"] });

    const { callbackResponse, sessionCookie } = await completeFlow();

    // 403, not 401: the credential was valid, the account is simply not allowed.
    expect(callbackResponse.status).toBe(403);
    expect(await callbackResponse.text()).toMatch(/not in a group allowed/i);
    expect(sessionCookie).toBeUndefined();
  });

  it("refuses a token that carries no groups claim at all", async () => {
    process.env.OIDC_ALLOWED_GROUPS = "Studio";
    oidc.setClaims({ groups: undefined });

    const { callbackResponse, sessionCookie } = await completeFlow();
    expect(callbackResponse.status).toBe(403);
    expect(sessionCookie).toBeUndefined();
  });

  it("re-checks the policy per request so tightening it applies at once", async () => {
    // Signed in while the allow-list allowed this user.
    process.env.OIDC_ALLOWED_GROUPS = "Onyx";
    oidc.setClaims({ groups: ["Onyx"] });
    const { sessionCookie } = await completeFlow();
    expect(sessionCookie).toBeTruthy();

    process.env.OMNIROUTE_API_KEY = "sk-valid-looking-key";

    // The list is tightened after the session was issued.
    process.env.OIDC_ALLOWED_GROUPS = "SomethingElse";
    const revoked = await generatePost(
      new Request("http://studio.test/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json", cookie: `${SESSION_COOKIE}=${sessionCookie}` },
        body: JSON.stringify({ prompt: "a counter" }),
      }),
    );
    expect(revoked.status).toBe(403);

    // Re-widening it restores access without a new login.
    process.env.OIDC_ALLOWED_GROUPS = "Onyx";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response('data: {"choices":[{"delta":{"content":"x"}}]}\n\n', { status: 200, headers: { "content-type": "text/event-stream" } })),
    );
    const restored = await generatePost(
      new Request("http://studio.test/api/generate", {
        method: "POST",
        headers: { "content-type": "application/json", cookie: `${SESSION_COOKIE}=${sessionCookie}` },
        body: JSON.stringify({ prompt: "a counter" }),
      }),
    );
    expect(restored.status).toBe(200);
  });
});
