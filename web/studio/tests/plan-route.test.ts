import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "@/app/api/plan/route";
import { DEFAULT_MODEL, resetModelCache } from "@/lib/omniroute";
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
    { id: DEFAULT_MODEL, owned_by: "combo" },
    { id: "openai/gpt-4o:free", owned_by: "openai" },
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
 * Stub the gateway, route by route.
 *
 * Three URLs are answered: the model catalogue, the connections list the
 * provider filter reads, and the completion the case supplies. They are matched
 * on the path rather than on call order, because a shared `Response` body can
 * only be read once — a positional stub would hand the catalogue read to the
 * connections call and leave the completion with an exhausted body. The
 * completion is cloned per call for the same reason, so a case that passes a
 * single `Response` can be asked for it more than once.
 */
function stubGateway(
  completion: Response | (() => Response),
  options: { connections?: unknown; connectionsStatus?: number } = {},
) {
  const mock = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const href = String(url);
    if (href.endsWith("/models")) {
      return Response.json(MODELS);
    }
    if (href.includes("/api/providers")) {
      if (options.connectionsStatus && options.connectionsStatus !== 200) {
        return new Response("no", { status: options.connectionsStatus });
      }
      return Response.json(options.connections ?? { connections: [{ provider: "combo" }, { provider: "openai" }] });
    }
    void init;
    return typeof completion === "function" ? completion() : completion.clone();
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
    expect(body.model).toBe(DEFAULT_MODEL);
  });

  it("asks the planner to decide what this is, rather than being told", async () => {
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({ prompt: "a shop" });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as {
      messages: { role: string; content: string }[];
    };
    expect(body.messages[0].content).toMatch(/YOU ALSO DECIDE WHAT THIS IS/);
  });

  it("ignores a kind a client still sends, so it cannot relabel the plan", async () => {
    // The picker is gone, but a tab left open from before it is not — and honouring
    // a stale answer is how a plan comes back disagreeing with its own kind.
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({ prompt: "a shop", kind: "website" });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as {
      messages: { role: string; content: string }[];
    };
    expect(body.messages[0].content).not.toMatch(/planning a (WEBSITE|FULL-STACK APPLICATION)/);
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

  it("budgets for the model's reasoning as well as for the plan", async () => {
    // The cap is what the model's hidden reasoning is billed against on a
    // reasoning model, so a plan-sized budget is not a plan-sized request. This
    // deployment measured 1,255–1,919 reasoning tokens for a three-file plan.
    const mock = stubGateway(completionResponse(GOOD_PLAN));
    await post({ prompt: "a tracker" });

    const completion = mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"));
    const body = JSON.parse(String(completion?.[1]?.body)) as { max_tokens: number };
    expect(body.max_tokens).toBeGreaterThanOrEqual(6_000);
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
    expect((await response.json()).error).toMatch(/rephrasing/);
  });

  it("calls an answer the gateway cut off by its length, not a malformed request", async () => {
    // Exactly what a reasoning model does to this turn: the JSON is unterminated
    // at the cap, so it fails to parse — but the request was fine and rephrasing
    // it will not help, which is what the old message told the person to do.
    const mock = stubGateway(() =>
      Response.json({
        choices: [
          {
            finish_reason: "length",
            message: { content: '{"name":"Tip Calculator","runtime":{"language":"no' },
          },
        ],
      }),
    );

    const response = await post({ prompt: "a tip calculator" });
    expect(response.status).toBe(422);

    const sent = JSON.parse(
      String(mock.mock.calls.find(([url]) => String(url).includes("/chat/completions"))?.[1]?.body),
    ) as { max_tokens: number };

    const error = (await response.json()).error as string;
    expect(error).toMatch(/cut off/);
    expect(error).toContain(sent.max_tokens.toLocaleString("en-US"));
    expect(error).toMatch(/nothing was written/i);
    expect(error).not.toMatch(/rephrasing/);
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

    // A *free* model on purpose: the paid ones are gated on a Magnate
    // entitlement this suite does not configure, and a test that asked for one
    // would be testing the gate rather than the model choice.
    const response = await post({ prompt: "a tracker", model: "openai/gpt-4o:free" });
    expect(response.status).toBe(200);
    expect((await response.json()).model).toBe("openai/gpt-4o:free");
  });

  it("refuses a model whose provider has no connection at the gateway", async () => {
    // "Linked in" means the provider has a connection, not merely that the gateway
    // can name it. `/v1/models` also lists the anonymous no-auth providers that
    // ship inside OmniRoute, and those are the ones that refuse a build.
    stubGateway(completionResponse(GOOD_PLAN), { connections: { connections: [{ provider: "combo" }] } });

    const response = await post({ prompt: "a tracker", model: "openai/gpt-4o:free" });
    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatch(/not one of the/);
  });

  it("still builds when the connection list cannot be read", async () => {
    // A read-only hiccup must not become an outage: with no connection list there
    // is nothing to filter against, so the gateway owns the answer.
    stubGateway(completionResponse(GOOD_PLAN), { connectionsStatus: 403 });

    const response = await post({ prompt: "a tracker", model: "openai/gpt-4o:free" });
    expect(response.status).toBe(200);
    expect((await response.json()).model).toBe("openai/gpt-4o:free");
  });
});
