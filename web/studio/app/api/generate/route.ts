import {
  buildMessages,
  chatCompletionsUrl,
  isPlaceholderSecret,
  readConfig,
  sseToTextStream,
  type PriorFile,
} from "@/lib/omniroute";
import { loadRepoEnv } from "@/lib/env";
import { isAuthorized, readAuthConfig, readSession } from "@/lib/auth";

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

  // Identity first: when OIDC is configured, only a signed-in operator builds.
  const authConfig = readAuthConfig();
  if (authConfig) {
    const session = readSession(request.headers.get("cookie"), authConfig);
    if (!session) return fail("Sign in to build with Studio.", 401);

    // Re-checked per request from the session's own groups, so tightening
    // OIDC_ALLOWED_GROUPS applies at once rather than at session expiry.
    if (!isAuthorized(session, authConfig)) {
      return fail("Your account is not in a group allowed to use Studio.", 403);
    }
  }

  // Optional shared gate. Set STUDIO_ACCESS_TOKEN to require a header; leave it
  // unset on a trusted network or behind an identity-aware proxy.
  loadRepoEnv();
  const accessToken = process.env.STUDIO_ACCESS_TOKEN?.trim();
  if (accessToken && request.headers.get("x-studio-token") !== accessToken) {
    return fail("Studio requires an access token. Add it under Settings.", 401);
  }

  const config = readConfig();
  if (isPlaceholderSecret(config.apiKey)) {
    return fail(
      "No gateway key is configured. Set OMNIROUTE_API_KEY in the repo .env and restart Studio.",
      503,
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
