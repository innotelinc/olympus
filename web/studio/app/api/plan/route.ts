import { chooseModel, completeChat, type TokenUsage } from "@/lib/omniroute";
import { PlanError, planMessages, parsePlan } from "@/lib/plan";
import { authorizeRequest, identityKey } from "@/lib/auth";
import { checkRateLimit } from "@/lib/ratelimit";
import { beginTurn, finishTurn } from "@/lib/tenancy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_PROMPT_CHARS = 8_000;
const MAX_PRIOR_FILES = 20;
const MAX_FILE_CHARS = 200_000;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: { "cache-control": "no-store" } });
}

function readPriorFiles(value: unknown): { path: string; contents: string }[] {
  if (!Array.isArray(value)) return [];

  const files: { path: string; contents: string }[] = [];

  for (const entry of value) {
    if (typeof entry !== "object" || entry === null) continue;

    const record = entry as Record<string, unknown>;
    const path = typeof record.path === "string" ? record.path.trim().slice(0, 200) : "";
    if (!path) continue;

    files.push({
      path,
      contents: typeof record.contents === "string" ? record.contents.slice(0, MAX_FILE_CHARS) : "",
    });
    if (files.length >= MAX_PRIOR_FILES) break;
  }

  return files;
}

/**
 * Plan a build before generating it.
 *
 * This is a separate route rather than a mode of `/api/generate` because the two
 * are different turns with different contracts: this one returns a small JSON
 * decision the user confirms, that one returns a stream of source files. Folding
 * them together would mean one route whose response shape depends on a flag, and a
 * plan is not something that streams.
 *
 * It shares `/api/generate`'s gates deliberately — identity, the same rate-limit
 * budget, the same model resolution — because a plan costs a model call too, and a
 * separate budget would be a way to spend two.
 */
export async function POST(request: Request): Promise<Response> {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return fail("Request body must be JSON.", 400);
  }

  const body = (payload ?? {}) as Record<string, unknown>;
  const prompt = typeof body.prompt === "string" ? body.prompt.trim() : "";

  if (!prompt) return fail("Describe what you want built.", 400);
  if (prompt.length > MAX_PROMPT_CHARS) {
    return fail(
      `That instruction is too long (${prompt.length} characters, limit ${MAX_PROMPT_CHARS}).`,
      413,
    );
  }

  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  // Whose key pays, and whether they may spend it — resolved before anything is
  // dispatched, so a refused turn costs nothing. Identity is strict (no silent
  // fallback to the shared key) and the quota check fails open; see lib/tenancy.
  const started = await beginTurn(gate);
  if (!started.ok) return started.response;
  const { turn } = started;
  const config = turn.config;

  const rate = checkRateLimit(identityKey(gate));
  if (!rate.ok) {
    return Response.json(
      { error: `Too many model requests from this account — try again in ${rate.retryAfterSeconds}s.` },
      {
        status: 429,
        headers: {
          "retry-after": String(rate.retryAfterSeconds),
          "cache-control": "no-store",
        },
      },
    );
  }

  // `body.kind` is deliberately ignored. The planner decides what this is and says
  // so in the plan; a client that still sends one is sending an answer from a
  // question that is no longer asked, and honouring it is how the plan and its kind
  // come to disagree.
  const priorFiles = readPriorFiles(body.files);

  const requestedModel = typeof body.model === "string" ? body.model.trim().slice(0, 200) : "";
  const chosen = await chooseModel(config, requestedModel);
  if (chosen.reject) {
    return fail(`${chosen.reject} Pick one from the list, or leave it on the default.`, 400);
  }

  let usage: TokenUsage | null = null;
  let reply: string;
  try {
    reply = await completeChat(config, {
      model: chosen.model,
      messages: planMessages(prompt, priorFiles),
      signal: request.signal,
      // A plan is a small object. The cap keeps a chatty model from spending a
      // generation's worth of tokens on it, and is generous enough that a long
      // file list still fits.
      maxTokens: 2_000,
      temperature: 0.2,
      // The gateway's own count, when it reports one. Recorded below.
      onUsage: (reported) => {
        usage = reported;
      },
    });
  } catch (error) {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    const status = typeof (error as { status?: unknown }).status === "number"
      ? ((error as { status: number }).status)
      : 502;
    return fail(error instanceof Error ? error.message : "The planner failed.", status);
  }

  // Recorded before the plan is validated, because the gateways charged for (and
  // the user spent) the turn whether or not the answer parsed.
  if (turn.caller) {
    const reported = usage as TokenUsage | null;
    await finishTurn(turn, {
      tokensIn: reported?.tokensIn ?? 0,
      tokensOut: reported?.tokensOut ?? 0,
      requests: 1,
      model: chosen.model,
    });
  }

  let plan;
  try {
    plan = parsePlan(reply);
  } catch (error) {
    // A malformed plan is a real failure with a real next step for the user
    // ("try rephrasing"), so the message is the planner's own, not a generic 500.
    if (error instanceof PlanError) return fail(error.message, 422);
    throw error;
  }

  return Response.json(
    { plan, model: chosen.model },
    { status: 200, headers: { "cache-control": "no-store" } },
  );
}
