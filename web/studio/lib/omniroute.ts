import { loadRepoEnv } from "./env";
import type { ProjectKind } from "./projects";

/**
 * OmniRoute client config. Every value is read server-side only — the gateway
 * key is never sent to, or bundled for, the browser.
 */

export type OmniRouteConfig = {
  baseUrl: string;
  chatPath: string;
  apiKey: string;
  model: string;
};

export type PriorFile = {
  path: string;
  contents: string;
};

/**
 * What a turn cost, when the gateway says so.
 *
 * Both shapes are read because the gateway can be configured for Chat
 * Completions (`prompt_/completion_tokens`) or the Responses API
 * (`input_/output_tokens`), and a turn that reported zero tokens because it was
 * counted under the other name would look like free usage in the ledger.
 */
export type TokenUsage = {
  tokensIn: number;
  tokensOut: number;
};

const DEFAULT_BASE_URL = "http://127.0.0.1:20128/v1";
const DEFAULT_CHAT_PATH = "/chat/completions";
const DEFAULT_MODEL = "auto/coding";

/** Values shipped in `.env.example` that must not be treated as real credentials. */
const PLACEHOLDER_PREFIXES = ["change-me", "changeme", "your-", "xxx", "todo"];

export function isPlaceholderSecret(value: string): boolean {
  const lowered = value.trim().toLowerCase();
  if (!lowered) return true;
  return PLACEHOLDER_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

export function readConfig(): OmniRouteConfig {
  loadRepoEnv();

  return {
    baseUrl: (process.env.OMNIROUTE_BASE_URL?.trim() || DEFAULT_BASE_URL).replace(/\/+$/, ""),
    chatPath: process.env.OMNIROUTE_CHAT_PATH?.trim() || DEFAULT_CHAT_PATH,
    apiKey: process.env.OMNIROUTE_API_KEY?.trim() || "",
    model: process.env.OMNIROUTE_MODEL?.trim() || DEFAULT_MODEL,
  };
}

export function chatCompletionsUrl(config: OmniRouteConfig): string {
  const path = config.chatPath.startsWith("/") ? config.chatPath : `/${config.chatPath}`;
  return `${config.baseUrl}${path}`;
}

/* ---- which model, out of whatever is linked in ---------------------------- */

/**
 * Studio does not have a model. The gateway does.
 *
 * Provider credentials live in OmniRoute, and the set of them changes without
 * Studio being touched — a key added this morning is a model this afternoon. So the
 * model is *discovered* from the gateway's own `/v1/models` rather than pinned in
 * code or in `.env`, which is the difference between "use whatever is linked in"
 * and a hardcoded id that quietly keeps working after the provider behind it is
 * swapped out.
 *
 * The list is large — 2,064 models across 23 providers, measured — so it is
 * trimmed to what a picker needs, and the `auto/*` combos come first: those are
 * OmniRoute's own routers, they do not require the user to know a provider's
 * naming, and they are the reason a default is a sensible thing to ship.
 */
export type GatewayModel = {
  id: string;
  provider: string;
  combo: boolean;
  contextLength: number | null;
  capabilities: Record<string, boolean>;
};

const MODEL_CACHE_MS = 60_000;

/** Module-level, because a picker page fetches this once and generation never blocks on it. */
let modelCache: { at: number; models: GatewayModel[] } | null = null;

/** Only for tests: a cached catalog would leak between cases. */
export function resetModelCache(): void {
  modelCache = null;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asCapabilities(value: unknown): Record<string, boolean> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return {};
  const result: Record<string, boolean> = {};
  for (const [key, flag] of Object.entries(value as Record<string, unknown>)) {
    if (typeof flag === "boolean") result[key] = flag;
  }
  return result;
}

/**
 * Read the gateway's model list. Pure, and defensive on purpose: this is a
 * response from another service, and one malformed entry must not empty a picker
 * that has 2,063 good ones behind it.
 */
export function parseModels(payload: unknown): GatewayModel[] {
  const raw = Array.isArray(payload)
    ? payload
    : typeof payload === "object" && payload !== null && Array.isArray((payload as Record<string, unknown>).data)
      ? ((payload as Record<string, unknown>).data as unknown[])
      : [];

  const models: GatewayModel[] = [];
  const seen = new Set<string>();

  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const record = entry as Record<string, unknown>;

    const id = asString(record.id);
    if (!id || seen.has(id)) continue;
    seen.add(id);

    const provider = asString(record.owned_by) || asString(record.ownedBy) || "unknown";
    const context = record.context_length ?? record.contextLength;

    models.push({
      id,
      provider,
      combo: provider === "combo",
      contextLength: typeof context === "number" && Number.isFinite(context) && context > 0 ? context : null,
      capabilities: asCapabilities(record.capabilities),
    });
  }

  // Combos first, then provider, then id. A stable, comparable order rather than
  // whatever the gateway happened to serialise, so the picker does not reshuffle
  // between two fetches a second apart.
  return models.sort((left, right) => {
    if (left.combo !== right.combo) return left.combo ? -1 : 1;
    if (left.provider !== right.provider) return left.provider < right.provider ? -1 : 1;
    return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
  });
}

/**
 * The linked models. Throws with a sentence a person can act on — the caller is a
 * route that turns it into a response, and the alternative is a picker that is
 * empty for no stated reason.
 */
export async function listModels(
  config: OmniRouteConfig,
  options: { fresh?: boolean; signal?: AbortSignal } = {},
): Promise<GatewayModel[]> {
  const now = Date.now();
  if (!options.fresh && modelCache && now - modelCache.at < MODEL_CACHE_MS) {
    return modelCache.models;
  }

  const url = `${config.baseUrl}/models`;
  let response: Response;
  try {
    response = await fetch(url, {
      headers: { authorization: `Bearer ${config.apiKey}` },
      signal: options.signal,
      cache: "no-store",
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    throw new Error(`Could not reach the gateway at ${config.baseUrl} (${detail}).`);
  }

  if (!response.ok) {
    const hint =
      response.status === 401 || response.status === 403
        ? " The configured OMNIROUTE_API_KEY was rejected."
        : "";
    throw new Error(`The gateway answered ${response.status} for its model list.${hint}`);
  }

  const models = parseModels(await response.json());
  modelCache = { at: now, models };
  return models;
}

/** Exact id first, then case-insensitively — a hand-typed `Auto/Coding` is the same model. */
export function findModel(models: GatewayModel[], id: string): GatewayModel | null {
  const wanted = id.trim();
  if (!wanted) return null;

  const exact = models.find((model) => model.id === wanted);
  if (exact) return exact;

  const lowered = wanted.toLowerCase();
  return models.find((model) => model.id.toLowerCase() === lowered) ?? null;
}

/**
 * The model to actually ask for, and where that choice came from.
 *
 * A requested model that is not in the linked list is a **400**, not a silent
 * fallback: the user picked it from a list Studio handed them, and quietly
 * building with a different model produces work they did not ask for and cannot
 * see. The caller decides what to do with the reason.
 */
export function resolveModel(
  config: OmniRouteConfig,
  models: GatewayModel[],
  requested: string,
): { model: string; source: "requested" | "configured" | "fallback"; reason?: string } {
  const modelsById = new Map(models.map((model) => [model.id, model]));

  if (requested) {
    const match = findModel(models, requested);
    if (!match) {
      return {
        model: config.model,
        source: "fallback",
        reason: `"${requested}" is not one of the ${models.length} models the gateway has linked in.`,
      };
    }
    return { model: match.id, source: "requested" };
  }

  if (modelsById.has(config.model)) return { model: config.model, source: "configured" };

  // The configured default is gone — the provider behind it was unlinked, or the
  // key was removed. `auto/coding` is OmniRoute's own router and survives that, so
  // it is the honest fallback; guessing a provider-specific id would not be.
  const combo = models.find((model) => model.id === DEFAULT_MODEL);
  return {
    model: combo?.id ?? config.model,
    source: "fallback",
    reason: `The configured model "${config.model}" is no longer linked in by the gateway.`,
  };
}

/**
 * The model a route should actually ask for, and whether the caller's choice was
 * one it could honour.
 *
 * Lives here rather than in each route because both the planning and the
 * generation turn spend the same pool, and two copies of "validate the picked
 * model" is how they drift — one of them would eventually accept a name the
 * gateway does not have and produce a confusing upstream error.
 *
 * A catalog that cannot be read is not allowed to break either route: with no
 * catalog there is nothing to validate against, so the caller's choice (or the
 * configured default) goes through and the gateway owns the answer. Failing here
 * would turn a read-only hiccup into an outage.
 */
export async function chooseModel(
  config: OmniRouteConfig,
  requested: string,
): Promise<{ model: string; reject: string | null }> {
  try {
    const models = await listModels(config);
    const resolved = resolveModel(config, models, requested);
    return { model: resolved.model, reject: requested && resolved.reason ? resolved.reason : null };
  } catch {
    return { model: requested || config.model, reject: null };
  }
}

/**
 * A failure talking to the gateway, with the status a route should answer.
 *
 * The gateway's own diagnosis is worth surfacing — it names the model and the
 * reason — but the upstream status is not the client's status, so a route maps
 * this rather than forwarding it. The request headers never appear in the
 * message; the key is in one of them.
 */
export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status: number = 502,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

/**
 * One non-streaming completion.
 *
 * Planning uses this rather than the SSE path because a plan is a small JSON
 * object that is only meaningful once it is whole: streaming it would buy a
 * partial plan, which is not something a person can confirm.
 */
export async function completeChat(
  config: OmniRouteConfig,
  options: {
    model: string;
    messages: Array<{ role: "system" | "user" | "assistant"; content: string }>;
    signal?: AbortSignal;
    temperature?: number;
    maxTokens?: number;
    /**
     * Called with the gateway's own token counts when it reports them.
     *
     * Optional and side-effect-only so the return type stays "the text": the
     * caller that pays for the turn (the plan route, reporting usage) and the
     * caller that only wants the answer are the same caller, and neither has to
     * branch on whether accounting is configured.
     */
    onUsage?: (usage: TokenUsage) => void;
  },
): Promise<string> {
  const url = chatCompletionsUrl(config);

  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({
        model: options.model,
        messages: options.messages,
        stream: false,
        temperature: options.temperature ?? 0.2,
        ...(options.maxTokens ? { max_tokens: options.maxTokens } : {}),
      }),
      signal: options.signal,
    });
  } catch (error) {
    if (options.signal?.aborted) throw new GatewayError("The request was cancelled.", 499);
    const detail = error instanceof Error ? error.message : "unknown error";
    throw new GatewayError(`Could not reach the gateway at ${config.baseUrl} (${detail}).`);
  }

  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.text()).slice(0, 400);
    } catch {
      detail = "";
    }
    const suffix = detail ? ` — ${detail}` : "";
    const hint =
      response.status === 401 || response.status === 403
        ? " The configured OMNIROUTE_API_KEY was rejected."
        : "";
    throw new GatewayError(
      `Gateway responded ${response.status} for model "${options.model}".${hint}${suffix}`,
    );
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new GatewayError("The gateway answered with something that is not JSON.");
  }

  const record = typeof payload === "object" && payload !== null ? (payload as Record<string, unknown>) : {};
  const usage = extractUsage(payload);
  if (usage && options.onUsage) options.onUsage(usage);

  const choices = Array.isArray(record.choices) ? record.choices : [];
  const first = (choices[0] ?? {}) as Record<string, unknown>;
  const message = (first.message ?? {}) as Record<string, unknown>;
  const content = message.content;

  if (typeof content === "string" && content.trim()) return content;

  // Some gateways answer the Responses shape instead of Chat Completions.
  if (typeof record.output_text === "string" && record.output_text.trim()) {
    return record.output_text;
  }

  throw new GatewayError("The gateway returned no text for that request.");
}

/**
 * Pull the text delta out of one SSE payload, tolerating both the Chat
 * Completions shape and the Responses API shape — the gateway can be
 * configured for either.
 */
function extractDelta(payload: unknown): string {
  if (typeof payload !== "object" || payload === null) return "";
  const record = payload as Record<string, unknown>;

  const choices = record.choices;
  if (Array.isArray(choices) && choices.length > 0) {
    const choice = choices[0] as Record<string, unknown> | undefined;
    const carrier = (choice?.delta ?? choice?.message) as Record<string, unknown> | undefined;
    const content = carrier?.content;
    if (typeof content === "string") return content;
    if (Array.isArray(content)) {
      return content
        .map((part) => {
          const text = (part as Record<string, unknown> | undefined)?.text;
          return typeof text === "string" ? text : "";
        })
        .join("");
    }
  }

  if (typeof record.delta === "string") return record.delta;
  if (typeof record.output_text === "string") return record.output_text;

  return "";
}

/**
 * The token counts a gateway payload reports, if it reports any.
 *
 * A streaming gateway is not required to send them, and one that does sends them
 * on the tail of the stream — so this is read from whatever chunk carries it and
 * a stream without it simply reports no usage rather than inventing a number.
 * (Distro's web app accounts for streaming turns the same way.)
 */
function extractUsage(payload: unknown): TokenUsage | null {
  if (typeof payload !== "object" || payload === null) return null;
  const usage = (payload as Record<string, unknown>).usage;
  if (typeof usage !== "object" || usage === null) return null;

  const record = usage as Record<string, unknown>;
  const read = (key: string): number | null => {
    const value = record[key];
    return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
  };

  const tokensIn = read("prompt_tokens") ?? read("input_tokens");
  const tokensOut = read("completion_tokens") ?? read("output_tokens");
  if (tokensIn === null && tokensOut === null) return null;

  return { tokensIn: tokensIn ?? 0, tokensOut: tokensOut ?? 0 };
}

function parseSseLine(line: string): { delta: string; usage: TokenUsage | null } {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data:")) return { delta: "", usage: null };

  const payload = trimmed.slice(5).trim();
  if (!payload || payload === "[DONE]") return { delta: "", usage: null };

  try {
    const parsed: unknown = JSON.parse(payload);
    return { delta: extractDelta(parsed), usage: extractUsage(parsed) };
  } catch {
    return { delta: "", usage: null };
  }
}

/**
 * Convert an SSE byte stream into a plain-text stream of generated source, so
 * the browser can accumulate and parse code blocks as they arrive.
 *
 * `onUsage` is how a streamed turn gets accounted for: the gateway's tail is the
 * only place a token count can come from when the response is a stream, and the
 * caller needs it after the answer, not before it.
 */
export function sseToTextStream(
  source: ReadableStream<Uint8Array>,
  options: { onUsage?: (usage: TokenUsage) => void } = {},
): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  let buffer = "";

  const consume = (line: string, controller: TransformStreamDefaultController<Uint8Array>): void => {
    const { delta, usage } = parseSseLine(line);
    if (usage && options.onUsage) options.onUsage(usage);
    if (delta) controller.enqueue(encoder.encode(delta));
  };

  return source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        buffer += decoder.decode(chunk, { stream: true });

        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) consume(line, controller);
      },
      flush(controller) {
        buffer += decoder.decode();
        consume(buffer, controller);
      },
    }),
  );
}

/**
 * Run `onDone` when the stream finishes.
 *
 * A streamed turn is reported after it ends, and the route has already returned
 * by then — so the accounting hangs off the stream's own completion rather than
 * off the handler. The callback runs once, on normal completion; a response the
 * client abandons never reaches `flush`.
 */
export function withStreamEnd(
  source: ReadableStream<Uint8Array>,
  onDone: () => void,
): ReadableStream<Uint8Array> {
  return source.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      flush() {
        onDone();
      },
    }),
  );
}
