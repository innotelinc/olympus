import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_MODEL,
  GatewayError,
  chatCompletionsUrl,
  chooseModel,
  completeChat,
  findModel,
  firstAvailableFreeModel,
  isModelRetired,
  isModelUnroutableRefusal,
  isPlaceholderSecret,
  listConnectedProviders,
  listModels,
  managementBaseUrl,
  normalizeBaseUrl,
  parseConnections,
  parseModels,
  readConfig,
  resetModelCache,
  resetRetiredModels,
  resolveModel,
  retireModel,
  sseToTextStream,
} from "@/lib/omniroute";

// readConfig() calls loadRepoEnv(), which reads the repo-root .env — and deleting a key
// from process.env does not make it absent, because the loader reads the file straight
// back. So without this the deployment's own values decide these assertions: a checkout
// that pins OMNIROUTE_MODEL failed "falls back to the default model" for a reason that
// has nothing to do with the code under test. Every test here sets what it needs.
// (env.test.ts covers the loader itself against temp directories.)
vi.mock("@/lib/env", () => ({ loadRepoEnv: () => {} }));

const MANAGED = ["OMNIROUTE_BASE_URL", "OMNIROUTE_API_KEY", "OMNIROUTE_MODEL", "OMNIROUTE_CHAT_PATH"];

let saved: Record<string, string | undefined> = {};

beforeEach(() => {
  saved = {};
  for (const key of MANAGED) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  for (const key of MANAGED) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
  vi.unstubAllGlobals();
  resetModelCache();
  resetRetiredModels();
});

beforeEach(() => {
  resetModelCache();
});

describe("isPlaceholderSecret", () => {
  it("treats empty and template values as unset", () => {
    expect(isPlaceholderSecret("")).toBe(true);
    expect(isPlaceholderSecret("   ")).toBe(true);
    expect(isPlaceholderSecret("change-me-omniroute-api-key")).toBe(true);
    expect(isPlaceholderSecret("CHANGE-ME")).toBe(true);
    expect(isPlaceholderSecret("your-api-key")).toBe(true);
  });

  it("accepts a real-looking key", () => {
    expect(isPlaceholderSecret("sk-abc123")).toBe(false);
  });
});

describe("readConfig", () => {
  it("falls back to the gateway door and the default model", () => {
    const config = readConfig();
    // The door (:20129), never the gateway's own port: inside this container
    // `127.0.0.1:20128` is Studio itself, which is how a rebuild once turned
    // every generation into ECONNREFUSED with nothing in the gateway's log.
    expect(config.baseUrl).toBe("http://192.168.1.46:20129/v1");
    expect(config.model).toBe(DEFAULT_MODEL);
    expect(config.chatPath).toBe("/chat/completions");
  });

  it("strips trailing slashes from the base URL", () => {
    process.env.OMNIROUTE_BASE_URL = "http://gateway:20128/v1///";
    expect(readConfig().baseUrl).toBe("http://gateway:20128/v1");
  });

  it("adds the /v1 to a bare door, which is how the gateway host exports it", () => {
    // Measured on this host: `/etc/profile.d/omniroute.sh` and `~/.bashrc` export
    // the door without `/v1`, and compose interpolation lets the shell win over
    // `.env`. Without this, every model list and completion 302s to Authentik.
    process.env.OMNIROUTE_BASE_URL = "http://192.168.1.46:20129";
    expect(readConfig().baseUrl).toBe("http://192.168.1.46:20129/v1");
  });

  it("honours overrides", () => {
    process.env.OMNIROUTE_MODEL = "auto/fast";
    process.env.OMNIROUTE_CHAT_PATH = "responses";
    const config = readConfig();
    expect(config.model).toBe("auto/fast");
    expect(chatCompletionsUrl(config)).toBe("http://192.168.1.46:20129/v1/responses");
  });
});

describe("chatCompletionsUrl", () => {
  it("joins the base URL and path", () => {
    expect(
      chatCompletionsUrl({
        baseUrl: "http://omniroute:20128/v1",
        chatPath: "/chat/completions",
        apiKey: "x",
        model: "auto/coding",
      }),
    ).toBe("http://omniroute:20128/v1/chat/completions");
  });
});

describe("normalizeBaseUrl", () => {
  it("adds a missing /v1", () => {
    expect(normalizeBaseUrl("http://gw:20129")).toBe("http://gw:20129/v1");
    expect(normalizeBaseUrl("http://gw:20129/")).toBe("http://gw:20129/v1");
  });

  it("leaves an existing /v1 alone", () => {
    expect(normalizeBaseUrl("http://gw:20129/v1")).toBe("http://gw:20129/v1");
    expect(normalizeBaseUrl("http://gw:20129/v1///")).toBe("http://gw:20129/v1");
  });

  it("keeps a path prefix", () => {
    expect(normalizeBaseUrl("https://gw/omniroute")).toBe("https://gw/omniroute/v1");
    expect(normalizeBaseUrl("https://gw/omniroute/v1")).toBe("https://gw/omniroute/v1");
  });
});

describe("managementBaseUrl", () => {
  function config(baseUrl: string) {
    return { baseUrl, chatPath: "/chat/completions", apiKey: "k", model: "m" };
  }

  it("drops the /v1 prefix the inference surface uses", () => {
    expect(managementBaseUrl(config("http://gw:20129/v1"))).toBe("http://gw:20129");
  });

  it("leaves a bare door and a trailing slash consistent", () => {
    expect(managementBaseUrl(config("http://gw:20129/"))).toBe("http://gw:20129");
    expect(managementBaseUrl(config("http://gw:20129"))).toBe("http://gw:20129");
  });
});

describe("parseConnections", () => {
  it("reads the connections envelope", () => {
    const providers = parseConnections({
      connections: [{ provider: "openai" }, { provider: "gemini" }, { provider: "openai" }],
    });
    expect([...(providers ?? [])]).toEqual(["openai", "gemini"]);
  });

  it("reads a bare array", () => {
    expect([...(parseConnections([{ provider: "openrouter" }]) ?? [])]).toEqual(["openrouter"]);
  });

  it("returns null for the model catalogue, which is not a connection list", () => {
    // The distinction is load-bearing: a `{ data: [...] }` envelope mistaken for
    // connections would filter every model away.
    expect(parseConnections({ data: [{ id: "a/b", owned_by: "a" }] })).toBeNull();
    expect(parseConnections(null)).toBeNull();
    expect(parseConnections("nope")).toBeNull();
  });

  it("ignores rows with no usable provider name", () => {
    const providers = parseConnections({ connections: [{}, { provider: "  " }, { provider: "x" }] });
    expect([...(providers ?? [])]).toEqual(["x"]);
  });

  // "The providers I have enabled" is the rule the picker is held to, and a
  // switched-off connection is not one of them. Measured on this gateway: the
  // OpenCode pair sits beside the live keys with isActive false, and offering its
  // models offers builds that cannot run.
  it("skips a connection the user switched off", () => {
    const providers = parseConnections({
      connections: [
        { provider: "opencode", isActive: false },
        { provider: "openrouter", isActive: true },
      ],
    });
    expect([...(providers ?? [])]).toEqual(["openrouter"]);
  });

  it("skips a connection in backoff, rate limited, or failed its own test", () => {
    const providers = parseConnections({
      connections: [
        { provider: "backed-off", isActive: true, backoffLevel: 2 },
        { provider: "rate-limited", isActive: true, rateLimitProtection: true },
        { provider: "failed", isActive: true, testStatus: "error" },
        { provider: "fine", isActive: true, testStatus: "active", backoffLevel: 0 },
      ],
    });
    expect([...(providers ?? [])]).toEqual(["fine"]);
  });

  it("keeps a provider that has one usable connection beside a dead one", () => {
    const providers = parseConnections({
      connections: [
        { provider: "openrouter", isActive: false },
        { provider: "openrouter", isActive: true },
      ],
    });
    expect([...(providers ?? [])]).toEqual(["openrouter"]);
  });
});

describe("listModels, filtered to the connected providers", () => {
  const config = { baseUrl: "http://gw:20129/v1", chatPath: "/chat/completions", apiKey: "k", model: "auto/coding" };

  const CATALOGUE = {
    data: [
      { id: "auto/coding", owned_by: "combo" },
      { id: "openrouter/auto", owned_by: "openrouter" },
      { id: "felo/felo-chat", owned_by: "felo-web" },
    ],
  };

  function stubRoutes(connections: unknown, status = 200) {
    const mock = vi.fn(async (url: string | URL) =>
      String(url).includes("/api/providers")
        ? Response.json(connections, { status })
        : Response.json(CATALOGUE),
    );
    vi.stubGlobal("fetch", mock);
    return mock;
  }

  it("keeps connected providers and the combos, and drops the rest", async () => {
    stubRoutes({ connections: [{ provider: "openrouter" }] });

    const models = await listModels(config, { fresh: true });
    expect(models.map((model) => model.id)).toEqual(["auto/coding", "openrouter/auto"]);
  });

  it("asks the management route at the gateway's root, not under /v1", async () => {
    const mock = stubRoutes({ connections: [{ provider: "openrouter" }] });

    await listModels(config, { fresh: true });
    const urls = mock.mock.calls.map(([url]) => String(url));
    expect(urls).toContain("http://gw:20129/api/providers?limit=5000");
  });

  it("leaves the catalogue alone when the connections cannot be read", async () => {
    stubRoutes("nope", 500);

    expect(await listModels(config, { fresh: true })).toHaveLength(3);
  });

  // `/v1/models` publishes models the gateway will not route — measured 2026-09-20,
  // every `gemini/*` entry answers 400 "not available in the active live catalog" —
  // so a model that has answered that way is dropped rather than offered again.
  it("drops a model the gateway has refused as outside its live catalogue", async () => {
    stubRoutes({ connections: [{ provider: "openrouter", isActive: true }] });
    retireModel("openrouter/auto");

    const models = await listModels(config, { fresh: true });
    expect(models.map((model) => model.id)).toEqual(["auto/coding"]);
  });
});

describe("refusals the gateway means", () => {
  // The deterministic one, verbatim from the gateway on 2026-09-20. This is the
  // only refusal worth believing about a *model*, because it does not depend on a
  // provider's mood.
  it("recognises the live-catalogue refusal", () => {
    expect(
      isModelUnroutableRefusal(400, '{"error":{"message":"Model \'gemini-3.7-flash\' is not available in the active live catalog for provider \'gemini\'","code":"model_not_found"}}'),
    ).toBe(true);
  });

  it("does not mistake a provider outage for a model that is gone", () => {
    // Every one of these is a provider having a bad day, measured against this
    // gateway: a key with nothing on it, a dead key, a rate limit, an upstream
    // that returned nothing. Hiding a model for these would empty the picker
    // during an outage that fixes itself.
    expect(isModelUnroutableRefusal(402, '{"error":{"message":"insufficient credits"}}')).toBe(false);
    expect(isModelUnroutableRefusal(401, '{"error":{"message":"unauthorized"}}')).toBe(false);
    expect(isModelUnroutableRefusal(429, '{"error":{"message":"rate limited"}}')).toBe(false);
    expect(isModelUnroutableRefusal(502, '{"error":{"message":"upstream returned an empty response without usable output"}}')).toBe(false);
    expect(isModelUnroutableRefusal(500, "<html>502 Bad Gateway</html>")).toBe(false);
  });

  it("remembers a refusal, and forgets it once the ttl is up", () => {
    const now = Date.now();
    retireModel("gemini/gemini-3-flash-preview", 1000, now);

    expect(isModelRetired("gemini/gemini-3-flash-preview", now + 500)).toBe(true);
    expect(isModelRetired("gemini/gemini-3-flash-preview", now + 1500)).toBe(false);
    expect(isModelRetired("openrouter/auto", now)).toBe(false);
  });
});

describe("firstAvailableFreeModel", () => {
  const models = parseModels({
    data: [
      { id: "auto/best-free", owned_by: "combo" },
      { id: "openai/gpt-4o", owned_by: "openai" },
      { id: "openrouter/x/free-model:free", owned_by: "openrouter" },
    ],
  });

  it("picks a free provider model, not a router that reports its own failures", () => {
    expect(firstAvailableFreeModel(models)?.id).toBe("openrouter/x/free-model:free");
  });

  it("skips one the gateway has refused", () => {
    retireModel("openrouter/x/free-model:free");
    expect(firstAvailableFreeModel(models)).toBeNull();
  });
});

describe("listConnectedProviders", () => {
  const config = { baseUrl: "http://gw:20129/v1", chatPath: "/chat/completions", apiKey: "k", model: "m" };

  it("returns null rather than throwing when the route refuses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("no", { status: 403 })));
    expect(await listConnectedProviders(config)).toBeNull();
  });

  it("returns null when the gateway cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connect ECONNREFUSED");
      }),
    );
    expect(await listConnectedProviders(config)).toBeNull();
  });
});

/* ---- streaming ---------------------------------------------------------- */

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function readAll(stream: ReadableStream<Uint8Array>): Promise<string> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let out = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    out += decoder.decode(value, { stream: true });
  }
  return out + decoder.decode();
}

function chatChunk(text: string): string {
  return `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`;
}

describe("sseToTextStream", () => {
  it("concatenates content deltas", async () => {
    const source = streamOf([chatChunk("<file "), chatChunk('path="a">'), chatChunk("x</file>")]);
    expect(await readAll(sseToTextStream(source))).toBe('<file path="a">x</file>');
  });

  it("reassembles a frame split across network chunks", async () => {
    const frame = chatChunk("hello");
    const cut = Math.floor(frame.length / 2);
    const source = streamOf([frame.slice(0, cut), frame.slice(cut)]);
    expect(await readAll(sseToTextStream(source))).toBe("hello");
  });

  it("ignores the [DONE] sentinel and non-data lines", async () => {
    const source = streamOf([chatChunk("a"), "event: message\n", ": keep-alive\n", "data: [DONE]\n\n"]);
    expect(await readAll(sseToTextStream(source))).toBe("a");
  });

  it("ignores malformed JSON frames", async () => {
    const source = streamOf(["data: {not json}\n\n", chatChunk("ok")]);
    expect(await readAll(sseToTextStream(source))).toBe("ok");
  });

  it("supports the Responses API delta shape", async () => {
    const source = streamOf([`data: ${JSON.stringify({ type: "response.output_text.delta", delta: "hi" })}\n\n`]);
    expect(await readAll(sseToTextStream(source))).toBe("hi");
  });

  it("supports array-shaped content parts", async () => {
    const source = streamOf([
      `data: ${JSON.stringify({ choices: [{ delta: { content: [{ type: "text", text: "part" }] } }] })}\n\n`,
    ]);
    expect(await readAll(sseToTextStream(source))).toBe("part");
  });

  it("preserves newlines inside generated code", async () => {
    const source = streamOf([chatChunk("<file>\n"), chatChunk("line1\nline2\n"), chatChunk("</file>")]);
    expect(await readAll(sseToTextStream(source))).toBe("<file>\nline1\nline2\n</file>");
  });

  it("emits a final frame that arrives without a trailing newline", async () => {
    const source = streamOf([chatChunk("a"), `data: ${JSON.stringify({ choices: [{ delta: { content: "z" } }] })}`]);
    expect(await readAll(sseToTextStream(source))).toBe("az");
  });
});

describe("parseModels", () => {
  it("reads the OpenAI-shaped list", () => {
    const models = parseModels({
      data: [{ id: "openai/gpt-4o", owned_by: "openai" }, { id: "auto/coding", owned_by: "combo" }],
    });

    expect(models.map((model) => model.id)).toEqual(["auto/coding", "openai/gpt-4o"]);
    expect(models[0].combo).toBe(true);
    expect(models[1].combo).toBe(false);
  });

  it("accepts a bare array as well as a data envelope", () => {
    expect(parseModels([{ id: "a/b", owned_by: "a" }])).toHaveLength(1);
  });

  it("drops entries with no id, and deduplicates", () => {
    const models = parseModels({ data: [{ owned_by: "x" }, { id: "a/b", owned_by: "a" }, { id: "a/b", owned_by: "a" }] });
    expect(models.map((model) => model.id)).toEqual(["a/b"]);
  });

  it("keeps a context length that is a positive number and ignores the rest", () => {
    const models = parseModels({
      data: [
        { id: "a/b", owned_by: "a", context_length: 128000 },
        { id: "c/d", owned_by: "c", context_length: "huge" },
        { id: "e/f", owned_by: "e", context_length: 0 },
      ],
    });

    const byId = new Map(models.map((model) => [model.id, model.contextLength]));
    expect(byId.get("a/b")).toBe(128000);
    expect(byId.get("c/d")).toBeNull();
    expect(byId.get("e/f")).toBeNull();
  });

  it("keeps only the boolean capabilities", () => {
    const models = parseModels({
      data: [{ id: "a/b", owned_by: "a", capabilities: { tools: true, vision: false, weird: "yes" } }],
    });
    expect(models[0].capabilities).toEqual({ tools: true, vision: false });
  });

  it("survives a payload that is not a list at all", () => {
    expect(parseModels(null)).toEqual([]);
    expect(parseModels("nope")).toEqual([]);
    expect(parseModels({ data: "nope" })).toEqual([]);
  });

  it("sorts combos first, then by provider, then by id", () => {
    const models = parseModels({
      data: [
        { id: "zzz/last", owned_by: "zzz" },
        { id: "aaa/first", owned_by: "aaa" },
        { id: "auto/coding", owned_by: "combo" },
      ],
    });
    expect(models.map((model) => model.id)).toEqual(["auto/coding", "aaa/first", "zzz/last"]);
  });
});

describe("findModel", () => {
  const models = parseModels({ data: [{ id: "auto/coding", owned_by: "combo" }] });

  it("matches exactly", () => {
    expect(findModel(models, "auto/coding")?.id).toBe("auto/coding");
  });

  it("matches case-insensitively, because a hand-typed name is the same model", () => {
    expect(findModel(models, "Auto/Coding")?.id).toBe("auto/coding");
  });

  it("returns null for an empty or unknown name", () => {
    expect(findModel(models, "")).toBeNull();
    expect(findModel(models, "nope")).toBeNull();
  });
});

describe("resolveModel", () => {
  const config = { baseUrl: "http://gw/v1", apiKey: "k", model: "auto/coding", chatPath: "/chat/completions" };
  const models = parseModels({
    data: [
      { id: "auto/coding", owned_by: "combo" },
      { id: DEFAULT_MODEL, owned_by: "openai" },
      { id: "openai/gpt-4o", owned_by: "openai" },
    ],
  });

  it("uses a requested model that is linked in", () => {
    expect(resolveModel(config, models, "openai/gpt-4o")).toEqual({ model: "openai/gpt-4o", source: "requested" });
  });

  it("reports a requested model that is not linked in rather than substituting one", () => {
    const resolved = resolveModel(config, models, "openai/gpt-5");
    expect(resolved.source).toBe("fallback");
    expect(resolved.reason).toMatch(/not one of the/);
  });

  it("falls back to the free default when the configured default is gone", () => {
    const resolved = resolveModel({ ...config, model: "retired/model" }, models, "");
    expect(resolved.model).toBe(DEFAULT_MODEL);
    expect(resolved.source).toBe("fallback");
    expect(resolved.reason).toMatch(/retired\/model/);
  });

  // The configured default can be a model the gateway still *lists* and will not
  // route — exactly the case that answered 502 for a person picking it. It is no
  // longer "the configured model", so the fallback takes over.
  it("does not keep using a configured default the gateway has refused", () => {
    retireModel(DEFAULT_MODEL);
    const withAnotherFree = parseModels({
      data: [
        { id: "auto/coding", owned_by: "combo" },
        { id: DEFAULT_MODEL, owned_by: "openrouter" },
        { id: "openrouter/other:free", owned_by: "openrouter" },
      ],
    });

    const resolved = resolveModel({ ...config, model: DEFAULT_MODEL }, withAnotherFree, "");
    expect(resolved.model).toBe("openrouter/other:free");
    expect(resolved.source).toBe("fallback");
    expect(resolved.reason).toMatch(/live catalogue/);
  });
});

describe("chooseModel", () => {
  const config = { baseUrl: "http://gw/v1", apiKey: "k", model: "auto/coding", chatPath: "/chat/completions" };

  it("rejects a model the gateway has not linked in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ data: [{ id: "auto/coding", owned_by: "combo" }] })));

    const chosen = await chooseModel(config, "openai/gpt-4o");
    expect(chosen.reject).toMatch(/not one of the/);
  });

  it("lets the request through when the catalogue cannot be read", async () => {
    // A read-only hiccup must not become an outage: with no catalogue there is
    // nothing to validate against, so the gateway owns the answer.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connect ECONNREFUSED");
      }),
    );

    const chosen = await chooseModel(config, "openai/gpt-4o");
    expect(chosen).toEqual({ model: "openai/gpt-4o", reject: null });
  });

  it("uses the configured default when the caller chose nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ data: [{ id: "auto/coding", owned_by: "combo" }] })));

    expect((await chooseModel(config, "")).model).toBe("auto/coding");
  });
});

describe("completeChat", () => {
  const config = { baseUrl: "http://gw/v1", apiKey: "k", model: "auto/coding", chatPath: "/chat/completions" };

  function stub(impl: () => Response | Promise<Response>) {
    const mock = vi.fn(impl);
    vi.stubGlobal("fetch", mock);
    return mock;
  }

  it("returns the assistant text", async () => {
    stub(() => Response.json({ choices: [{ message: { content: '{"ok":true}' } }] }));
    expect(await completeChat(config, { model: "m", messages: [] })).toBe('{"ok":true}');
  });

  it("sends the key as a bearer header and asks for a non-streaming answer", async () => {
    const mock = stub(() => Response.json({ choices: [{ message: { content: "x" } }] }));

    await completeChat(config, { model: "m", messages: [{ role: "user", content: "hi" }] });

    const [url, init] = mock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toBe("http://gw/v1/chat/completions");
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer k");
    expect(JSON.parse(String(init.body)).stream).toBe(false);
  });

  it("reads the Responses shape too", async () => {
    stub(() => Response.json({ output_text: "hello" }));
    expect(await completeChat(config, { model: "m", messages: [] })).toBe("hello");
  });

  it("throws a GatewayError with the gateway's own diagnosis", async () => {
    stub(() => new Response("model not found", { status: 404 }));

    const error = await completeChat(config, { model: "m", messages: [] }).catch((thrown: unknown) => thrown);
    expect(error).toBeInstanceOf(GatewayError);
    expect((error as Error).message).toMatch(/404/);
    expect((error as Error).message).toMatch(/model not found/);
  });

  it("hints at the key when the gateway refuses it", async () => {
    stub(() => new Response("unauthorized", { status: 401 }));

    const error = (await completeChat(config, { model: "m", messages: [] }).catch((e: unknown) => e)) as Error;
    expect(error.message).toMatch(/OMNIROUTE_API_KEY/);
  });

  it("retires a model the gateway does not have, and says so", async () => {
    stub(
      () =>
        new Response(
          '{"error":{"message":"Model \'gemini-3-flash-preview\' is not available in the active live catalog for provider \'gemini\'"}}',
          { status: 400 },
        ),
    );

    const error = (await completeChat(config, { model: "gemini/gemini-3-flash-preview", messages: [] }).catch((e: unknown) => e)) as Error;
    expect(error).toBeInstanceOf(GatewayError);
    expect(error.message).toMatch(/dropped from the model list/);
    expect(isModelRetired("gemini/gemini-3-flash-preview")).toBe(true);
  });

  it("does not retire a model over a provider's bad day", async () => {
    stub(() => new Response('{"error":{"message":"upstream returned an empty response"}}', { status: 502 }));

    await completeChat(config, { model: "gemini/gemini-3-flash-preview", messages: [] }).catch(() => undefined);
    expect(isModelRetired("gemini/gemini-3-flash-preview")).toBe(false);
  });

  it("throws when there is no usable text", async () => {
    stub(() => Response.json({ choices: [] }));
    await expect(completeChat(config, { model: "m", messages: [] })).rejects.toThrow(/no text/);
  });

  it("throws when the body is not JSON", async () => {
    stub(() => new Response("<html>gateway</html>", { status: 200 }));
    await expect(completeChat(config, { model: "m", messages: [] })).rejects.toThrow(/not JSON/);
  });

  it("reports an unreachable gateway with its address", async () => {
    stub(() => {
      throw new Error("connect ECONNREFUSED");
    });

    await expect(completeChat(config, { model: "m", messages: [] })).rejects.toThrow(/Could not reach the gateway/);
  });

  it("reports the gateway's own reason for stopping", async () => {
    // The text still comes back — it is the caller that decides a `length` answer
    // is unusable, which is what lets the plan route say "cut off" rather than
    // reporting its own half-parsed object as a malformed request.
    stub(() => Response.json({ choices: [{ finish_reason: "length", message: { content: '{"a":' } }] }));

    const reasons: string[] = [];
    const text = await completeChat(config, {
      model: "m",
      messages: [],
      onFinishReason: (reason) => reasons.push(reason),
    });

    expect(text).toBe('{"a":');
    expect(reasons).toEqual(["length"]);
  });

  it("stays quiet when the gateway names no reason", async () => {
    stub(() => Response.json({ choices: [{ message: { content: '{"ok":true}' } }] }));

    const onFinishReason = vi.fn();
    await completeChat(config, { model: "m", messages: [], onFinishReason });

    expect(onFinishReason).not.toHaveBeenCalled();
  });
});
