import {
  buildMessages,
  chatCompletionsUrl,
  isPlaceholderSecret,
  readConfig,
  sseToTextStream,
  type PriorFile,
} from "@/lib/omniroute";
import { authorizeRequest } from "@/lib/auth";
import { createHash } from "node:crypto";
import { checkRateLimit } from "@/lib/ratelimit";

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

  const config = readConfig();
  if (isPlaceholderSecret(config.apiKey)) {
    return fail(
      "No gateway key is configured. Set OMNIROUTE_API_KEY in the repo .env and restart Studio.",
      503,
    );
  }

  // Every generation bills the shared model pool, so each identity gets a
  // bounded number of attempts per minute. OIDC callers are keyed by their
  // verified session subject; token callers by a hash of the token, so two
  // operators with different tokens get separate budgets without the secret
  // ever becoming a map key. Input and auth are already checked; this is the
  // last gate before the only request that costs money.
  const identity = gate.session?.sub
    ? `oidc:${gate.session.sub}`
    : `token:${createHash("sha256").update(process.env.STUDIO_ACCESS_TOKEN ?? "").digest("hex").slice(0, 16)}`;
  const rate = checkRateLimit(identity);
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
  const url = chatCompletionsUrl(config);

  let upstream: Response;
  try {
    upstream = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${config.apiKey}`,
      },
      body: JSON.stringify({
        model: config.model,
        messages: buildMessages(prompt, priorFiles),
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
    const hint =
      upstream.status === 401 || upstream.status === 403
        ? " The configured OMNIROUTE_API_KEY was rejected."
        : "";
    return fail(`Gateway responded ${upstream.status} for model "${config.model}".${hint}${suffix}`, 502);
  }

  const contentType = upstream.headers.get("content-type") ?? "";
  const stream = contentType.includes("text/event-stream")
    ? sseToTextStream(upstream.body as ReadableStream<Uint8Array>)
    : (upstream.body as ReadableStream<Uint8Array>);

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
