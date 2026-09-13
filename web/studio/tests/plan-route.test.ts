import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/plan/route";
import { resetModelCache } from "@/lib/omniroute";
import { resetRateLimits } from "@/lib/ratelimit";

// The route calls loadRepoEnv(), which reads the repo-root .env. Deleting a key
// from process.env does not make it absent — the loader puts it straight back
// from the file. On a configured checkout that silently turns OIDC on and the
// auth gate answers 401 before the checks under test ever run, so the loader is
// neutralized here and every test sets exactly the variables it needs.
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

const MANAGED = [
  "OMNIROUTE_API_KEY",
  "OMNIROUTE_BASE_URL",
  "OMNIROUTE_MODEL",
  "STUDIO_ACCESS_TOKEN",
  "STUDIO_RATE_LIMIT_PER_MIN",
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
  resetModelCache();
  resetRateLimits();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
  resetModelCache();
  resetRateLimits();
});

function post(payload: unknown, headers: Record<string, string> = {}): Promise<Response> {
  return POST(
    new Request("http://studio.test/api/plan", {
      method: "POST",
      headers: { "content-type": "application/json", ...headers },
      body: typeof payload === "string" ? payload : JSON.stringify(payload),
    }),
  );
}

const MODELS = {
  data: [
    { id: "auto/coding", owned_by: "combo" },
    { id: "openai/gpt-4o", owned_by: "openai" },
  ],
};

const GOOD_PLAN = JSON.stringify({
  name: "Weight Tracker",
  summary: "Daily weigh-ins with a small React client.",
  runtime: { language: "node", frameworks: ["react"], database: "sqlite" },
  run: { install: "npm install", build: "npm run build", start: "npm start", port: 3000 },
  files: [{ path: "package.json", purpose: "Dependencies" }],
  notes: null,
});

/**
 * Stub the gateway: the model catalog always answers, and the completion
 * answers whatever the case needs. Returns the mock so a test can assert on the
 * request that was actually sent.
 */
function stubGateway(completion: Response | (() => Response)) {
  const mock = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    if (href.endsWith("/models")) {
      return Response.json(MODELS);
    }
    void init;
    return typeof completion === "function" ? completion() : completion;
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}

function completionResponse(text: string): Response {
  return Response.json({ choices: [{ message: { content: text } }] });
}

describe("validation", () => {
  it("rejects a malformed body", async () => {
    const response = await post("not json");
    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatch(/must be JSON/i);
  });

  it("rejects an empty prompt", async () => {
    expect((await post({})).status).toBe(400);
  });

  it("rejects an oversized prompt", async () => {
    const response = await post({ prompt: "x".repeat(9000) });
    expect(response.status).toBe(413);
  });
});

describe("gates", () => {
  it("returns 503 when no gateway key is configured", async () => {
    process.env.OMNIROUTE_API_KEY = "";
    const response = await post({ prompt: "a tracker" });
    expect(response.status).toBe(503);
  });

  it("requires a session when OIDC is configured", async () => {
    process.env.OIDC_ISSUER_URL = "http://idp.test";
    process.env.OIDC_CLIENT_ID = "studio";
    process.env.OIDC_CLIENT_SECRET = "not-a-placeholder";

    const response = await post({ prompt: "a tracker" });
    expect(response.status).toBe(401);
  });

  it("rate limits a caller that spends its budget", async () => {
    process.env.STUDIO_RATE_LIMIT_PER_MIN = "1";
    stubGateway(completionResponse(GOOD_PLAN));

    expect((await post({ prompt: "a tracker" })).status).toBe(200);

    const limited = await post({ prompt: "a tracker" });
    expect(limited.status).toBe(429);
    expect(limited.headers.get("retry-after")).toBeTruthy();
  });
});

describe("planning", () => {
  it("returns a parsed plan", async () => {
    stubGateway(completionResponse(GOOD_PLAN));

    const response = await post({ prompt: "a weight tracker" });
    expect(response.status).toBe(200);

    const body = await response.json();
    expect(body.plan.slug).toBe("weight-tracker");
    expect(body.plan.run.start).toBe("npm start");
    expect(body.model).toBe("auto/coding");
  });

  it("passes the kind through to the prompt", async () => {
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({ prompt: "a shop", kind: "website" });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as {
      messages: { role: string; content: string }[];
    };
    expect(body.messages[0].content).toMatch(/WEBSITE/);
  });

  it("sends an existing project as context, and asks for an addition", async () => {
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({
      prompt: "add a chart",
      files: [{ path: "src/App.tsx", contents: "export default function App(){return null}" }],
    });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as {
      messages: { role: string; content: string }[];
    };
    expect(body.messages).toHaveLength(3);
    expect(body.messages[1].content).toMatch(/add a chart|already exists/);
    expect(body.messages[2].content).toBe("add a chart");
  });

  it("asks for one non-streaming completion", async () => {
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({ prompt: "a tracker" });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as { stream: boolean };
    expect(body.stream).toBe(false);
  });

  it("refuses a reply with no usable start command", async () => {
    stubGateway(completionResponse('{"name":"x","run":{"start":""}}'));

    const response = await post({ prompt: "a tracker" });
    expect(response.status).toBe(422);
    expect((await response.json()).error).toMatch(/start command/);
  });

  it("refuses a reply that is not a plan at all", async () => {
    stubGateway(completionResponse("Sure! Here is what I would build..."));

    const response = await post({ prompt: "a tracker" });
    expect(response.status).toBe(422);
  });

  it("returns 502 when the gateway errors on the completion", async () => {
    stubGateway(() => new Response("upstream exploded", { status: 500 }));

    const response = await post({ prompt: "a tracker" });
    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/500/);
  });

  it("rejects a model the gateway has not linked in", async () => {
    stubGateway(completionResponse(GOOD_PLAN));

    const response = await post({ prompt: "a tracker", model: "openai/gpt-5-turbo" });
    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatch(/not one of the/);
  });

  it("accepts a model the gateway has linked in", async () => {
    stubGateway(completionResponse(GOOD_PLAN));

    const response = await post({ prompt: "a tracker", model: "openai/gpt-4o" });
    expect(response.status).toBe(200);
    expect((await response.json()).model).toBe("openai/gpt-4o");
  });
});
