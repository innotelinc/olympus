import { authorizeRequest } from "@/lib/auth";
import { isPlaceholderSecret, listModels, readConfig, resolveModel } from "@/lib/omniroute";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The models this deployment can actually build with — whatever is linked in.
 *
 * Studio owns no provider credentials and no model list of its own. Provider keys
 * live in OmniRoute, and the set of them changes without Studio being redeployed, so
 * the honest answer to "what can I build with" is the gateway's own answer. This
 * route asks it and hands back a shaped subset.
 *
 * The gateway key never leaves this process: the browser gets model *names*, which
 * are not secrets, and none of them is usable without the key on this side.
 *
 * `?fresh=1` bypasses the one-minute cache, for the case that motivated the whole
 * thing — a key was just added in OmniRoute and the picker should not lie about it
 * for another minute.
 */
export async function GET(request: Request): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  const config = readConfig();
  if (isPlaceholderSecret(config.apiKey)) {
    return Response.json(
      { error: "No gateway key is configured. Set OMNIROUTE_API_KEY in the repo .env and restart Studio." },
      { status: 503, headers: { "cache-control": "no-store" } },
    );
  }

  const url = new URL(request.url);
  const fresh = url.searchParams.get("fresh") === "1";

  let models;
  try {
    models = await listModels(config, { fresh });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    return Response.json(
      { error: `Could not read the gateway's model list: ${detail}` },
      { status: 502, headers: { "cache-control": "no-store" } },
    );
  }

  const resolved = resolveModel(config, models, "");
  const providers = [...new Set(models.map((model) => model.provider))].sort();

  return Response.json(
    {
      models,
      providers,
      // What Studio will use when the caller does not choose, and why — so the
      // picker can say "default" against a real entry rather than guessing.
      default: resolved.model,
      configured: config.model,
      note: resolved.reason ?? null,
    },
    {
      status: 200,
      headers: {
        // Private: the list is the same for every viewer, but this is an
        // authenticated route and a shared cache must not hold it.
        "cache-control": "private, max-age=60",
      },
    },
  );
}
