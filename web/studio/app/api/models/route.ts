import { authorizeRequest } from "@/lib/auth";
import { entitlementFor, readEntitlementConfig } from "@/lib/entitlements";
import {
  MODEL_PRESETS,
  isFreeModelId,
  isPlaceholderSecret,
  listModels,
  modelAllowedFor,
  readConfig,
  resolveModel,
} from "@/lib/omniroute";

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

  // Paid models are for subscribers. The decision is Magnate's (see
  // lib/entitlements): without a definite "entitled", the picker offers the free
  // routers and the free connections only, and both model routes refuse a paid id
  // if one is sent anyway — a picker is a courtesy, not a gate.
  const entitlement = await entitlementFor(readEntitlementConfig(), gate.session?.email ?? "");
  const visible = models.filter((model) => modelAllowedFor(model.id, entitlement.paid));

  return Response.json(
    {
      models: visible,
      providers,
      // What Studio will use when the caller does not choose, and why — so the
      // picker can say "default" against a real entry rather than guessing.
      default: resolved.model,
      configured: config.model,
      note: resolved.reason ?? null,
      paid: entitlement.paid,
      entitlement: entitlement.reason,
      plan: entitlement.plan,
      presets: MODEL_PRESETS.map((preset) => ({
        ...preset,
        allowed: modelAllowedFor(preset.id, entitlement.paid),
      })),
      hiddenPaidModels: models.length - visible.length,
      freeOnly: !entitlement.paid,
      // Why the list is short, in the words the picker can show verbatim.
      accessNote: entitlement.paid
        ? null
        : "Free models only: your account has no active subscription for this plan. " +
          "Paid models appear once Magnate says the subscription is active.",
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
