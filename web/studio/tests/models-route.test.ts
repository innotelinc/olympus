import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "@/app/api/models/route";
import { DEFAULT_MODEL, resetModelCache } from "@/lib/omniroute";

// See plan-route.test.ts: the repo .env loader would otherwise put OIDC settings
// back after a test deletes them.
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
  resetModelCache();
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
  resetModelCache();
});

function get(query = ""): Promise<Response> {
  return GET(new Request(`http://studio.test/api/models${query}`));
}

function stubCatalog(payload: unknown, status = 200) {
  let calls = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      calls += 1;
      return Response.json(payload, { status });
    }),
  );
  return () => calls;
}

const CATALOG = {
  data: [
    { id: "openai/gpt-4o:free", owned_by: "openai", context_length: 128000 },
    { id: DEFAULT_MODEL, owned_by: "combo" },
    { id: "anthropic/claude-3-5:free", owned_by: "anthropic", context_length: 200000 },
  ],
};

describe("gates", () => {
  it("returns 503 when no gateway key is configured", async () => {
    process.env.OMNIROUTE_API_KEY = "";
    expect((await get()).status).toBe(503);
  });

  it("requires a session when OIDC is configured", async () => {
    process.env.OIDC_ISSUER_URL = "http://idp.test";
    process.env.OIDC_CLIENT_ID = "studio";
    process.env.OIDC_CLIENT_SECRET = "not-a-placeholder";

    expect((await get()).status).toBe(401);
  });
});

describe("the catalogue", () => {
  it("lists combos first, then by provider", async () => {
    stubCatalog(CATALOG);

    const body = await (await get()).json();
    expect(body.models.map((model: { id: string }) => model.id)).toEqual([
      DEFAULT_MODEL,
      "anthropic/claude-3-5:free",
      "openai/gpt-4o:free",
    ]);
  });

  it("names the providers it found", async () => {
    stubCatalog(CATALOG);

    const body = await (await get()).json();
    expect(body.providers).toEqual(["anthropic", "combo", "openai"]);
  });

  it("reports what Studio will use when nothing is chosen", async () => {
    stubCatalog(CATALOG);

    const body = await (await get()).json();
    expect(body.default).toBe(DEFAULT_MODEL);
    expect(body.configured).toBe(DEFAULT_MODEL);
  });

  it("serves the cached list without asking the gateway twice", async () => {
    const calls = stubCatalog(CATALOG);

    await get();
    await get();
    expect(calls()).toBe(1);
  });

  it("bypasses the cache for ?fresh=1, which is the point of it", async () => {
    const calls = stubCatalog(CATALOG);

    await get();
    await get("?fresh=1");
    expect(calls()).toBe(2);
  });

  it("never lets a shared cache hold it", async () => {
    stubCatalog(CATALOG);

    const response = await get();
    expect(response.headers.get("cache-control")).toBe("private, max-age=60");
  });
});

describe("failures", () => {
  it("returns 502 when the gateway refuses the key", async () => {
    stubCatalog({ error: "unauthorized" }, 401);

    const response = await get();
    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/401|rejected/i);
  });

  it("returns 502 when the gateway is unreachable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connect ECONNREFUSED");
      }),
    );

    const response = await get();
    expect(response.status).toBe(502);
    expect((await response.json()).error).toMatch(/could not reach/i);
  });

  it("does not cache a failure, so a fixed gateway is picked up at once", async () => {
    stubCatalog({ error: "boom" }, 500);
    expect((await get()).status).toBe(502);

    stubCatalog(CATALOG);
    expect((await get()).status).toBe(200);
  });
});
