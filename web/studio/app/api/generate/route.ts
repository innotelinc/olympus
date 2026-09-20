import {
  chatCompletionsUrl,
  chooseModel,
  isModelUnroutableRefusal,
  noteModelRefusal,
  sseToTextStream,
  withStreamEnd,
  type PriorFile,
  type TokenUsage,
} from "@/lib/omniroute";
import { PlanError, generationMessages, parsePlanObject } from "@/lib/plan";
import { parseKind } from "@/lib/projects";
import { authorizeRequest, identityKey } from "@/lib/auth";
import { checkRateLimit } from "@/lib/ratelimit";
import { beginTurn, finishTurn } from "@/lib/tenancy";
import { entitlementFor, readEntitlementConfig } from "@/lib/entitlements";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_PROMPT_CHARS = 8_000;
const MAX_PRIOR_FILES = 20;
const MAX_FILE_CHARS = 200_000;
const MAX_ERROR_CHARS = 400;

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status });
}

function readPriorFiles(value: unknown): PriorFile[] {
  if (!Array.isArray(value)) return [];

  const files: PriorFile[] = [];

  for (const entry of value) {
    if (typeof entry !== "object" || entry === null) continue;

    const record = entry as Record<string, unknown>;
    const path = typeof record.path === "string" ? record.path.trim().slice(0, 200) : "";
    if (!path) continue;

    const contents =
      typeof record.contents === "string" ? record.contents.slice(0, MAX_FILE_CHARS) : "";

    files.push({ path, contents });
    if (files.length >= MAX_PRIOR_FILES) break;
  }

  return files;
}

export async function POST(request: Request): Promise<Response> {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return fail("Request body must be JSON.", 400);
  }

  const body = (payload ?? {}) as Record<string, unknown>;
  const prompt = typeof body.prompt === "string" ? body.prompt.trim() : "";

  if (!prompt) return fail("Describe the app you want to build.", 400);
  if (prompt.length > MAX_PROMPT_CHARS) {
    return fail(`That instruction is too long (${prompt.length} characters, limit ${MAX_PROMPT_CHARS}).`, 413);
  }

  // Identity first, then the optional shared token — one gate, shared with the
  // saved-app routes so the two cannot drift apart.
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  // Whose key pays, and whether they may spend it. Resolved before the stream is
  // opened: a refused turn must not reach the gateway at all. See lib/tenancy for
  // why identity is strict and the quota check is not.
  const started = await beginTurn(gate);
  if (!started.ok) return started.response;
  const { turn } = started;
  const config = turn.config;

  // Every generation bills the shared model pool, so each identity gets a
  // bounded number of attempts per minute. OIDC callers are keyed by their
  // verified session subject; token callers by a hash of the token, so two
  // operators with different tokens get separate budgets without the secret
  // ever becoming a map key. Input and auth are already checked; this is the
  // last gate before the only request that costs money.
  const rate = checkRateLimit(identityKey(gate));
  if (!rate.ok) {
    return Response.json(
      {
        error: `Too many generations from this account — try again in ${rate.retryAfterSeconds}s.`,
      },
      {
        status: 429,
        headers: {
          "retry-after": String(rate.retryAfterSeconds),
          "cache-control": "no-store",
        },
      },
    );
  }

  const priorFiles = readPriorFiles(body.files);

  // The plan is required, and it is re-validated rather than trusted: it has been
  // to the browser and back. Refusing a missing plan keeps one contract instead of
  // two — a request without one would fall back to a stack the user never saw.
  //
  // The kind comes from the plan, not from the request: the planner decided what
  // this is when it read the request, and the plan is the only thing that went to
  // the browser and back. A `body.kind` from an older client is ignored rather than
  // allowed to relabel a plan it does not match.
  let plan;
  try {
    plan = parsePlanObject(body.plan);
  } catch (error) {
    return fail(
      error instanceof PlanError
        ? `That build plan is not usable: ${error.message} Plan it again from the prompt.`
        : "That build plan is not usable. Plan it again from the prompt.",
      400,
    );
  }

  const url = chatCompletionsUrl(config);

  // The model comes from the caller, because the picker lists what the gateway has
  // linked in and that list changes without Studio being redeployed. It is checked
  // against that list rather than trusted: a name that is not linked in is a 400
  // here, where the user can see it, instead of a gateway error mid-stream or —
  // worse — a silent fallback that builds something they did not ask for.
  //
  // The catalog being unreachable is not allowed to break generation, though: with
  // no catalog there is nothing to validate against, so the request goes through
  // and the gateway owns the answer. Failing here would turn a read-only hiccup
  // into an outage.
  const requestedModel = typeof body.model === "string" ? body.model.trim().slice(0, 200) : "";
  // Paid models are for subscribers; the picker hides the rest, and this is what
  // makes that true rather than decorative. See lib/entitlements for why an
  // unreachable Magnate reads as "not paid".
  const entitlement = await entitlementFor(
    readEntitlementConfig(),
    turn.caller?.email ?? gate.session?.email ?? "",
  );
  const chosen = await chooseModel(config, requestedModel, { allowPaid: entitlement.paid });
  if (chosen.reject) {
    return fail(`${chosen.reject} Pick one from the list, or leave it on the default.`, 400);
  }
  const model = chosen.model;

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({
        model,
        messages: generationMessages(prompt, priorFiles, plan),
        stream: true,
        temperature: 0.4,
      }),
      signal: request.signal,
    });
  } catch (error) {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    const detail = error instanceof Error ? error.message : "unknown error";
    return fail(`Could not reach the gateway at ${config.baseUrl} (${detail}).`, 502);
  }

  if (!upstream.ok) {
    // Surface the gateway's own diagnosis, bounded — never the request headers.
    let detail = "";
    try {
      detail = (await upstream.text()).slice(0, MAX_ERROR_CHARS);
    } catch {
      detail = "";
    }
    const suffix = detail ? ` — ${detail}` : "";
    // A model the gateway does not have in its live catalogue is retired here, so
    // the next read of the picker stops offering the one the user just proved does
    // not work. Only the deterministic refusal is treated this way — a provider
    // having a bad day must not delete its models from the list.
    const unroutable = isModelUnroutableRefusal(upstream.status, detail);
    if (unroutable) noteModelRefusal(model, upstream.status, detail);
    const hint = unroutable
      ? " That model is not in the gateway's live catalogue, so it has been dropped from the model list."
      : upstream.status === 401 || upstream.status === 403
        ? " The configured OMNIROUTE_API_KEY was rejected."
        : "";
    return fail(`Gateway responded ${upstream.status} for model "${model}".${hint}${suffix}`, 502);
  }

  // The turn is reported when the stream ends, not when the handler returns — a
  // stream has not been paid for until it has been generated. Token counts come
  // from the gateway's own tail when it sends them (see extractUsage); a gateway
  // that sends none still gets the request recorded.
  let usage: TokenUsage | null = null;
  const upstreamBody = upstream.body as ReadableStream<Uint8Array>;
  const reportTurn = (): void => {
    const reported = usage as TokenUsage | null;
    void finishTurn(turn, {
      tokensIn: reported?.tokensIn ?? 0,
      tokensOut: reported?.tokensOut ?? 0,
      requests: 1,
      model,
    });
  };

  const contentType = upstream.headers.get("content-type") ?? "";
  const stream = withStreamEnd(
    contentType.includes("text/event-stream")
      ? sseToTextStream(upstreamBody, {
          onUsage: (reported) => {
            usage = reported;
          },
        })
      : upstreamBody,
    reportTurn,
  );

  return new Response(stream, {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store, no-transform",
      // Keep intermediaries (and the NPM edge) from buffering the stream.
      "x-accel-buffering": "no",
    },
  });
}
