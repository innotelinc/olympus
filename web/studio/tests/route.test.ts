import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/generate/route";

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
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
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

describe("validation", () => {
  it("rejects a malformed body", async () => {
    const response = await post("not json");
    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatch(/must be JSON/i);
  });

  it("rejects an empty prompt", async () => {
    await expect(post("{}")).resolves.toHaveProperty("status", 400);
  });

  it("rejects a whitespace-only prompt", async () => {
    const response = await post(JSON.stringify({ prompt: "   " }));
    expect(response.status).toBe(400);
  });

  it("rejects an oversized prompt", async () => {
    const response = await post(JSON.stringify({ prompt: "x".repeat(9000) }));
    expect(response.status).toBe(413);
    expect((await response.json()).error).toMatch(/too long/i);
  });
});

describe("configuration gate", () => {
  it("returns 503 when no gateway key is configured", async () => {
    process.env.OMNIROUTE_API_KEY = "";
    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(503);
    expect((await response.json()).error).toMatch(/OMNIROUTE_API_KEY/);
  });

  it("returns 503 for a template placeholder key", async () => {
    process.env.OMNIROUTE_API_KEY = "change-me-omniroute-api-key";
    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(503);
  });
});

describe("access control", () => {
  it("requires a session when OIDC is configured", async () => {
    process.env.OIDC_ISSUER_URL = "http://idp.test";
    process.env.OIDC_CLIENT_ID = "studio";
    process.env.OIDC_CLIENT_SECRET = "not-a-placeholder";

    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(401);
    expect((await response.json()).error).toMatch(/sign in/i);
  });

  it("requires the access token header when STUDIO_ACCESS_TOKEN is set", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";

    const missing = await post(JSON.stringify({ prompt: "a counter" }));
    expect(missing.status).toBe(401);

    const wrong = await post(JSON.stringify({ prompt: "a counter" }), { "x-studio-token": "nope" });
    expect(wrong.status).toBe(401);
  });

  it("accepts the correct access token", async () => {
    process.env.STUDIO_ACCESS_TOKEN = "shared-secret";
    stubFetch(async () => sseResponse(['data: {"choices":[{"delta":{"content":"ok"}}]}\n\n', "data: [DONE]\n\n"]));

    const response = await post(JSON.stringify({ prompt: "a counter" }), { "x-studio-token": "shared-secret" });
    expect(response.status).toBe(200);
  });
});

describe("streaming", () => {
  it("converts the gateway SSE stream into plain source text", async () => {
    stubFetch(async () =>
      sseResponse([
        'data: {"choices":[{"delta":{"content":"<file path=\\"index.html\\">"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"<html></html>"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"</file>"}}]}\n\n',
        "data: [DONE]\n\n",
      ]),
    );

    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/plain");
    expect(await response.text()).toBe('<file path="index.html"><html></html></file>');
  });

  it("passes through a non-SSE JSON response untouched", async () => {
    stubFetch(async () => new Response('{"ok":true}', { status: 200, headers: { "content-type": "application/json" } }));

    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(await response.text()).toBe('{"ok":true}');
  });

  it("forwards prior files so revisions keep context", async () => {
    const mock = stubFetch(async () => sseResponse(['data: {"choices":[{"delta":{"content":"x"}}]}\n\n']));

    await post(
      JSON.stringify({
        prompt: "make the button blue",
        files: [{ path: "index.html", contents: "<button>go</button>" }],
      }),
    );

    const body = JSON.parse(String(mock.mock.calls[0][1]?.body));
    expect(body.stream).toBe(true);
    expect(body.model).toBe("auto/coding");
    expect(JSON.stringify(body.messages)).toContain("<button>go</button>");
    expect(body.messages.at(-1).content).toBe("make the button blue");
  });

  it("drops malformed entries from the prior-file list", async () => {
    const mock = stubFetch(async () => sseResponse(['data: {"choices":[{"delta":{"content":"x"}}]}\n\n']));

    await post(JSON.stringify({ prompt: "go", files: [null, 42, { path: "" }, { path: "ok.js", contents: "1" }] }));

    const body = JSON.parse(String(mock.mock.calls[0][1]?.body));
    expect(JSON.stringify(body.messages)).toContain("ok.js");
  });

  it("sends the key as a bearer header", async () => {
    const mock = stubFetch(async () => sseResponse(['data: {"choices":[{"delta":{"content":"x"}}]}\n\n']));
    await post(JSON.stringify({ prompt: "a counter" }));

    const [url, init] = mock.mock.calls[0] as [string, RequestInit & { headers: Record<string, string> }];
    expect(url).toContain("/chat/completions");
    expect(init.headers.authorization).toBe("Bearer sk-valid-looking-key");
  });
});

describe("upstream failures", () => {
  it("relays a rejected key as 502 with a hint", async () => {
    stubFetch(async () => new Response('{"error":{"message":"Authentication required"}}', { status: 401 }));

    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(502);
    const { error } = await response.json();
    expect(error).toContain("401");
    expect(error).toMatch(/rejected/i);
  });

  it("relays an unreachable gateway as 502", async () => {
    stubFetch(async () => {
      throw new Error("ECONNREFUSED");
    });

    const response = await post(JSON.stringify({ prompt: "a counter" }));
    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/Could not reach the gateway/i);
  });

  it("does not leak the key or authorization header in error text", async () => {
    stubFetch(async () => new Response("upstream boom", { status: 500 }));
    const response = await post(JSON.stringify({ prompt: "a counter" }));
    const { error } = await response.json();

    expect(error).not.toContain("sk-valid-looking-key");
    expect(error.toLowerCase()).not.toContain("authorization");
  });
});
