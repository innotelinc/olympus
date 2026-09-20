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

/**
 * The platform gateway's **door**, not the gateway.
 *
 * This default used to be `http://127.0.0.1:20128/v1`, and inside this container
 * that is Studio talking to itself: the gateway is a group-2 service on its own
 * host, published on that host's loopback and bridge alone because
 * `requireLogin=false` makes reaching `20128` the whole control. The routable
 * address is the identity-aware proxy in front of it (`20129`), which exempts
 * `/v1` for API clients — they send a key, not a session cookie.
 *
 * A deployment sets `OMNIROUTE_BASE_URL` either way; the default is what an
 * unset one gets, and a fallback that resolves to nothing is worse than a
 * fallback that names the one address every host can reach. On the gateway's own
 * host, `http://host.docker.internal:20129/v1` is the same door.
 */
const DEFAULT_BASE_URL = "http://192.168.1.46:20129/v1";
const DEFAULT_CHAT_PATH = "/chat/completions";
/**
 * What an unset `OMNIROUTE_MODEL` means, and therefore what every build uses
 * unless somebody chooses otherwise. It is a *free* model, because a build that
 * spends nothing is the right posture for a tool people are invited to try and a
 * paid default turns a first experiment into a provider bill.
 *
 * It is a named free model rather than OmniRoute's `auto/best-free` router, and
 * that is a measured choice, not a preference. Called on this gateway on
 * 2026-09-19, `auto/best-free` returns **502**: its candidate chain is dominated
 * by free providers that cannot serve the gateway — the OpenCode free tier
 * refuses outright (`403 … can only be used from within OpenCode`) and Felo's
 * free tier fails at thread creation (`400`/`429`) — and the router reports the
 * accumulated failures rather than falling through to a provider that works.
 * `auto/best-free` stays in `MODEL_PRESETS`, where it is still the right label for
 * "the strongest free provider"; it just cannot be what an unset default resolves
 * to while it answers with an error. See `docs/stack.md`.
 */
export const DEFAULT_MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free";

/**
 * The presets the picker offers above the raw model list.
 *
 * "Preset" means a gateway *router* (`auto/…`), not a provider's model id: those
 * survive a provider key being unlinked, and they are the only ids a user can pick
 * without knowing which provider today's best model happens to live on. The free
 * ones are marked, because that is the distinction the entitlement gate turns on.
 */
export const MODEL_PRESETS: readonly {
  id: string;
  label: string;
  note: string;
  free: boolean;
}[] = [
  {
    id: "auto/best-free",
    label: "Best free model",
    note: "the strongest provider that needs no paid key — the default",
    free: true,
  },
  {
    id: "auto/coding:free",
    label: "Best free model for code",
    note: "free-only, weighted for code",
    free: true,
  },
  {
    id: "auto/best-fast",
    label: "Fastest available",
    note: "latency first, paid providers included",
    free: false,
  },
  {
    id: "auto/best-coding",
    label: "Best for code",
    note: "paid providers included",
    free: false,
  },
  {
    id: "auto/coding",
    label: "Balanced router",
    note: "OmniRoute's general-purpose coding router",
    free: false,
  },
];

/**
 * Is this a model that costs nothing to call?
 *
 * Read from the id, because that is what the gateway publishes: its free routers
 * end in `:free`, and its free provider connections carry `free` in the model name
 * (`oc/deepseek-v4-flash-free`). A paid model that happens to include the word
 * would be a provider naming a paid tier "free", which would be a problem in the
 * gateway long before it is one here.
 */
export function isFreeModelId(id: string): boolean {
  const value = id.trim().toLowerCase();
  if (!value) return false;
  const preset = MODEL_PRESETS.find((entry) => entry.id === value);
  if (preset) return preset.free;
  return value.endsWith(":free") || /(^|[/:._-])free([/:._-]|$)/.test(value);
}

/** May this id be chosen by a caller with (or without) a paid subscription? */
export function modelAllowedFor(id: string, paid: boolean): boolean {
  return paid || isFreeModelId(id);
}

/** Values shipped in `.env.example` that must not be treated as real credentials. */
const PLACEHOLDER_PREFIXES = ["change-me", "changeme", "your-", "xxx", "todo"];

export function isPlaceholderSecret(value: string): boolean {
  const lowered = value.trim().toLowerCase();
  if (!lowered) return true;
  return PLACEHOLDER_PREFIXES.some((prefix) => lowered.startsWith(prefix));
}

/**
 * The inference base URL, whether or not the caller remembered the `/v1`.
 *
 * `OMNIROUTE_BASE_URL` is written two ways across this estate and both mean the
 * same door: the repository's `/v1` form, and the bare door that the host's own
 * `profile.d`/`~/.bashrc` export. Compose interpolation gives the *shell*
 * environment precedence over `.env`, so on a host that exports the bare form the
 * bare form wins — and a bare door silently turns `/models` and
 * `/chat/completions` into routes the SSO proxy answers with a login redirect
 * (measured: `/models` -> 302, and Studio reported the gateway "answer[ing] 400"
 * for a model list that never reached it). Appending the missing `/v1` makes both
 * spellings work rather than making the deployment depend on which one won.
 *
 * Only the suffix is added: a base that already ends in `/v1` — including one
 * behind a path prefix, `https://gw/omniroute/v1` — is left exactly as it came.
 */
export function normalizeBaseUrl(url: string): string {
  const trimmed = url.trim().replace(/\/+$/, "");
  if (!trimmed) return trimmed;
  return /\/v1$/.test(trimmed) ? trimmed : `${trimmed}/v1`;
}

export function readConfig(): OmniRouteConfig {
  loadRepoEnv();

  return {
    baseUrl: normalizeBaseUrl(process.env.OMNIROUTE_BASE_URL?.trim() || DEFAULT_BASE_URL),
    chatPath: process.env.OMNIROUTE_CHAT_PATH?.trim() || DEFAULT_CHAT_PATH,
    apiKey: process.env.OMNIROUTE_API_KEY?.trim() || "",
    model: process.env.OMNIROUTE_MODEL?.trim() || DEFAULT_MODEL,
  };
}

export function chatCompletionsUrl(config: OmniRouteConfig): string {
  const path = config.chatPath.startsWith("/") ? config.chatPath : `/${config.chatPath}`;
  return `${config.baseUrl}${path}`;
}

/**
 * The gateway's management API, which lives at its root and not under `/v1`.
 *
 * `OMNIROUTE_BASE_URL` names the OpenAI-compatible surface (`…/v1`, or the bare
 * door, depending on the deployment), and `/api/providers` is a sibling of that
 * prefix rather than a child of it. So the address is the same door with a
 * trailing `/v1` removed — the same rule `scripts/omniroute-restore-providers.py`
 * applies, for the same reason.
 */
export function managementBaseUrl(config: OmniRouteConfig): string {
  return config.baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "");
}

/**
 * A `testStatus` that means the connection failed its own test, rather than one
 * that says nothing useful. The gateway's field is free-form, so only the values
 * that plainly mean "this cannot serve" are listed; an unknown word is left alone
 * rather than guessed at, because the cost of guessing wrong is a picker that
 * hides a provider the user did pay for.
 */
const UNUSABLE_TEST_STATUSES = new Set(["error", "failed", "invalid", "unauthorized", "expired"]);

/**
 * Can this connection serve a request? Only the gateway knows, and this reads the
 * three things it publishes about it: whether the connection is switched on, whether
 * it is in backoff, and whether it is sitting out a rate limit. A provider with two
 * connections is offered if *either* is usable — one dead key beside a live one is a
 * provider that works.
 */
function connectionIsUsable(row: Record<string, unknown>): boolean {
  if (row.isActive === false) return false;
  const backoff = row.backoffLevel;
  if (typeof backoff === "number" && backoff > 0) return false;
  if (row.rateLimitProtection === true) return false;
  return !UNUSABLE_TEST_STATUSES.has(asString(row.testStatus).toLowerCase());
}

/**
 * The provider ids that have a connection **the user enabled and the gateway can
 * currently use**, out of `GET /api/providers`.
 *
 * `null` means "could not tell" — the payload was not the connections shape, or
 * the call failed — and every caller must read that as *do not filter*, never as
 * *nothing is connected*. The distinction is the whole point: a gateway that
 * briefly refuses a read-only route must not empty the model picker or pin a
 * default, so an unreadable list leaves the catalogue exactly as the gateway
 * published it.
 *
 * A connection that is switched off is not part of "the providers I have
 * enabled", which is the rule the picker is held to: this estate has a disabled
 * OpenCode pair sitting beside the live keys, and offering its models is offering a
 * build that cannot run. The same goes for a connection in backoff, sitting out a
 * rate limit, or failed its own test. What this cannot see is a *model* the
 * gateway will not route — that is per-model and only shows up in an answer, so it
 * is learned instead (see `retireModel`).
 *
 * Only `connections` is read. A bare array is the other shape the endpoint has
 * been seen to answer with; a `{ data: […] }` envelope is the *model* list, and
 * treating it as connections would filter every model away on a test stub that
 * answers both calls with the catalogue.
 */
export function parseConnections(payload: unknown): Set<string> | null {
  const rows = Array.isArray(payload)
    ? payload
    : typeof payload === "object" && payload !== null && Array.isArray((payload as Record<string, unknown>).connections)
      ? ((payload as Record<string, unknown>).connections as unknown[])
      : null;
  if (!rows) return null;
  const providers = new Set<string>();
  for (const row of rows) {
    if (typeof row !== "object" || row === null) continue;
    const record = row as Record<string, unknown>;
    const provider = asString(record.provider);
    if (provider && connectionIsUsable(record)) providers.add(provider);
  }
  return providers;
}

/**
 * Ask the gateway which providers it actually has connected.
 *
 * This is the read-only half of the SSO door (`--skip-auth-route=^/api/providers$`
 * in `compose.gateway-sso.yml`); through any other address the management API is
 * behind the Authentik session and unreachable from a bridge container, which is
 * why the repository raises this route at the door rather than reaching the
 * gateway's own loopback port.
 */
export async function listConnectedProviders(
  config: OmniRouteConfig,
  options: { signal?: AbortSignal } = {},
): Promise<Set<string> | null> {
  try {
    const response = await fetch(`${managementBaseUrl(config)}/api/providers?limit=5000`, {
      headers: { authorization: `Bearer ${config.apiKey}` },
      signal: options.signal,
      cache: "no-store",
    });
    if (!response.ok) return null;
    return parseConnections(await response.json());
  } catch {
    return null;
  }
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

/* ---- models the gateway has told us it cannot route ---------------------- */

/**
 * How long a refusal is remembered. Long enough that a picker stops offering the
 * model to the person who just hit it, short enough that a provider coming back — a
 * key topped up, a rate limit expiring, Google rotating a preview model back — is
 * offered again without anyone restarting Studio.
 */
const RETIRED_TTL_MS = 30 * 60_000;

let retiredModels = new Map<string, number>();

/** Only for tests: a remembered refusal would leak between cases. */
export function resetRetiredModels(): void {
  retiredModels.clear();
}

/**
 * Remember that the gateway refused this model. Drops the cached catalogue too, so
 * the next read of the picker is filtered rather than waiting out the cache.
 */
export function retireModel(id: string, ttlMs = RETIRED_TTL_MS, now = Date.now()): void {
  const key = id.trim();
  if (!key) return;
  retiredModels.set(key, now + ttlMs);
  modelCache = null;
}

export function isModelRetired(id: string, now = Date.now()): boolean {
  const key = id.trim();
  const until = retiredModels.get(key);
  if (until === undefined) return false;
  if (until <= now) {
    retiredModels.delete(key);
    return false;
  }
  return true;
}

/**
 * Is this the gateway saying "that model is not in my live catalogue"?
 *
 * Worth telling apart from its other refusals, because it is the deterministic one.
 * Measured on this gateway (2026-09-20) across every model it lists: the ones it
 * cannot route answer `400` with `Model 'x' is not available in the active live
 * catalog for provider 'y'`, while a provider that is merely unhappy answers with
 * its own status — `401` for a dead key, `402` for a key with nothing on it, `406`,
 * `429`, `500`, or a `502` whose body says the upstream returned nothing. The first
 * is a model to stop offering; the rest are a provider having a bad day, and
 * hiding a model for those would empty the picker during an outage that fixes
 * itself.
 */
export function isModelUnroutableRefusal(status: number, body: string): boolean {
  if (status !== 400 && status !== 404) return false;
  return /not available in the active live catalog|model_not_found/i.test(body);
}

/** Retire the model when the refusal is the deterministic one. */
export function noteModelRefusal(id: string, status: number, body: string): void {
  if (isModelUnroutableRefusal(status, body)) retireModel(id);
}

/**
 * The free model to use when nobody chose one.
 *
 * The fallback follows the same rule the picker is held to — free, and from a
 * provider this deployment has enabled — rather than a hardcoded id that may since
 * have been unlinked or refused. Combos are skipped because they are routers, and
 * on this gateway `auto/best-free` reports its candidate chain's failures (Felo
 * `400`/`429`) instead of falling through to a provider that works.
 */
export function firstAvailableFreeModel(models: GatewayModel[]): GatewayModel | null {
  return (
    models.find((model) => !model.combo && !isModelRetired(model.id) && isFreeModelId(model.id)) ?? null
  );
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
 * The linked models, trimmed to the providers that are actually connected.
 *
 * Throws with a sentence a person can act on — the caller is a route that turns it
 * into a response, and the alternative is a picker that is empty for no stated
 * reason. A readable connection list is what makes this "the models a build can
 * actually use": `/v1/models` alone cannot tell a credentialed provider from the
 * anonymous no-auth ones bundled with the gateway, so the connections are read
 * alongside it and the catalogue is filtered to them.
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

  const catalogue = parseModels(await response.json());
  // "Whatever is linked in" means the providers with a *connection*, not every
  // provider the gateway can name. `/v1/models` also lists the anonymous no-auth
  // providers that ship inside OmniRoute (Felo, DuckDuckGo, Auggie, the Codex
  // app-server), and those are exactly the ones that refuse a build — the OpenCode
  // `403` and Felo `400`/`429` that took `auto/best-free` down on 2026-09-19
  // (docs/stack.md). Filtering them out is what makes "the providers I have
  // connected" the picker's rule. A list that cannot be read (`null`) filters
  // nothing, so a hiccup on the read-only route cannot empty the catalogue.
  const connected = await listConnectedProviders(config, { signal: options.signal });
  const linked =
    connected && connected.size > 0
      ? catalogue.filter((model) => model.combo || connected.has(model.provider))
      : catalogue;
  // And a model the gateway has already refused for being outside its live
  // catalogue is not offered again — that is the half of "available" `/v1/models`
  // cannot answer, because it lists models the gateway will not route (measured:
  // every `gemini/*` and `agentrouter/*` entry it publishes answers `400`, "not
  // available in the active live catalog").
  const models = linked.filter((model) => !isModelRetired(model.id));
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
        model: firstAvailableFreeModel(models)?.id ?? config.model,
        source: "fallback",
        reason: `"${requested}" is not one of the ${models.length} models the gateway has linked in.`,
      };
    }
    return { model: match.id, source: "requested" };
  }

  if (modelsById.has(config.model) && !isModelRetired(config.model)) {
    return { model: config.model, source: "configured" };
  }

  // The configured default is gone — the provider behind it was unlinked, the key
  // was removed, or the gateway has since refused it as unroutable. A free model
  // from an enabled provider is the honest replacement: it is what the picker is
  // allowed to offer, and it is what an unset default already resolves to.
  return {
    model: firstAvailableFreeModel(models)?.id ?? config.model,
    source: "fallback",
    reason: isModelRetired(config.model)
      ? `The gateway does not have "${config.model}" in its live catalogue.`
      : `The configured model "${config.model}" is no longer linked in by the gateway.`,
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
  options: { allowPaid?: boolean } = {},
): Promise<{ model: string; reject: string | null }> {
  // Paid models are for subscribers, and the check is here rather than in the
  // picker: a route that trusts the browser's list is a route that spends money on
  // a model the caller did not pay for, one hand-written request at a time.
  const allowPaid = options.allowPaid !== false;
  try {
    const models = await listModels(config);
    const resolved = resolveModel(config, models, requested);
    if (!allowPaid && !isFreeModelId(resolved.model)) {
      if (requested) {
        return {
          model: resolved.model,
          reject: "That model needs an active subscription (your account has none for this plan).",
        };
      }
      // Nobody asked for a paid model — the default is one. Fall back instead of
      // failing: an unset `OMNIROUTE_MODEL` must not be able to bill a
      // non-subscriber, and "best free model" is what they get.
      return { model: DEFAULT_MODEL, reject: null };
    }
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
    /**
     * Called with the gateway's own reason for stopping, when it reports one.
     *
     * `length` is the one worth knowing about. This deployment's default model is
     * a reasoning one, and its hidden reasoning is billed as output tokens against
     * `max_tokens` — measured on the plan turn, 1,255–1,919 of a 2,000-token budget
     * went to reasoning alone. So a `length` answer is a *truncated* answer, and
     * the caller that reads a JSON object out of it can say "cut off" instead of
     * reporting its own half-parsed object as a bad request.
     */
    onFinishReason?: (reason: string) => void;
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
    // A model outside the gateway's live catalogue is retired before the error is
    // raised, so the picker stops offering the thing the caller just proved does
    // not work. Only the deterministic refusal counts; see isModelUnroutableRefusal.
    const unroutable = isModelUnroutableRefusal(response.status, detail);
    if (unroutable) noteModelRefusal(options.model, response.status, detail);
    const hint = unroutable
      ? " That model is not in the gateway's live catalogue, so it has been dropped from the model list."
      : response.status === 401 || response.status === 403
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

  if (typeof first.finish_reason === "string" && options.onFinishReason) {
    options.onFinishReason(first.finish_reason);
  }

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
