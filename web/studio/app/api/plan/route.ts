import { chooseModel, completeChat, type TokenUsage } from "@/lib/omniroute";
import { PlanError, planMessages, parsePlan } from "@/lib/plan";
import { authorizeRequest, identityKey } from "@/lib/auth";
import { checkRateLimit } from "@/lib/ratelimit";
import { beginTurn, finishTurn } from "@/lib/tenancy";
import { entitlementFor, readEntitlementConfig } from "@/lib/entitlements";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_PROMPT_CHARS = 8_000;
const MAX_PRIOR_FILES = 20;
const MAX_FILE_CHARS = 200_000;

/**
 * The planner's output budget, in tokens.
 *
 * It was 2,000, chosen when "a plan is a small object" was the whole story. It is
 * not: on the model this deployment pins (`gemini/gemini-3-flash-preview`) the
 * hidden reasoning is billed as output tokens against this same cap, so a plain
 * "tip calculator" request was measured spending 1,255–1,919 of the 2,000 on
 * reasoning and **cut off mid-object** — which arrives here as an unterminated
 * `{…` with no closing brace, and used to be reported to the person as "the
 * planner did not return a JSON plan. Try rephrasing the request." That message
 * blamed their phrasing for a budget that the model's own thinking had already
 * spent, and it was roughly a coin flip per request.
 *
 * The plan itself is genuinely small (~500 tokens for three files), so this cap is
 * room for reasoning plus a plan, not room for a plan. It is a ceiling rather than
 * a spend — the provider bills what is generated — so raising it costs nothing on
 * the requests that finish. `onFinishReason` below is what keeps a truncation
 * legible if some future model reasons even longer.
 */
const PLAN_MAX_TOKENS = 8_000;

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
  const entitlement = await entitlementFor(
    readEntitlementConfig(),
    turn.caller?.email ?? gate.session?.email ?? "",
  );
  const chosen = await chooseModel(config, requestedModel, { allowPaid: entitlement.paid });
  if (chosen.reject) {
    return fail(`${chosen.reject} Pick one from the list, or leave it on the default.`, 400);
  }

  let usage: TokenUsage | null = null;
  let finishReason = "";
  let reply: string;
  try {
    reply = await completeChat(config, {
      model: chosen.model,
      messages: planMessages(prompt, priorFiles),
      signal: request.signal,
      maxTokens: PLAN_MAX_TOKENS,
      temperature: 0.2,
      // The gateway's own count, when it reports one. Recorded below.
      onUsage: (reported) => {
        usage = reported;
      },
      // Read so a truncated answer can be told apart from a malformed one; see
      // the parse failure below.
      onFinishReason: (reason) => {
        finishReason = reason;
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
    if (error instanceof PlanError) {
      // A reply the gateway stopped for length is not a malformed request, and
      // saying so is the difference between "try again" and "rephrase it". With a
      // reasoning model this is the whole budget being spent before the JSON
      // closes, and the fix is a retry (reasoning length varies) or a model
      // without extended thinking — not different wording from the person.
      if (finishReason === "length") {
        return fail(
          `The planner was cut off at ${PLAN_MAX_TOKENS.toLocaleString("en-US")} tokens before the ` +
            "plan closed — with a reasoning model most of that budget goes to the model's own " +
            "thinking rather than to the plan. Nothing was written. Try again, or pick a model " +
            "without extended thinking.",
          422,
        );
      }

      // A malformed plan is a real failure with a real next step for the user
      // ("try rephrasing"), so the message is the planner's own, not a generic 500.
      return fail(error.message, 422);
    }
    throw error;
  }

  return Response.json(
    { plan, model: chosen.model },
    { status: 200, headers: { "cache-control": "no-store" } },
  );
}
