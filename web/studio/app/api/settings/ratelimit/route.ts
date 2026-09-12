import { authorizeRequest } from "@/lib/auth";
import { loadRepoEnv } from "@/lib/env";
import {
  MAX_RATE_LIMIT_PER_MIN,
  readRateLimitState,
  writeRateLimitOverride,
  type RateLimitState,
} from "@/lib/ratelimit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The generation rate limit, as an operator control.
 *
 * `STUDIO_RATE_LIMIT_PER_MIN` is the deployment default; this endpoint stores
 * a runtime override beside the saved apps so the limit can be switched off
 * (or set to a different number) without editing `.env` and restarting.
 * Clearing the override hands control back to the environment, so the
 * deployment default is always one click away.
 *
 * The override is **deployment-wide, not per-identity**: it decides the limit
 * every caller is held to. That is the right shape for an operator setting, and
 * it is why the write is gated the same way generation is — an authenticated
 * caller only, which behind OIDC means someone in `OIDC_ALLOWED_GROUPS`.
 * Studio is an operator tool behind an identity-aware proxy; anyone who can
 * build here can already spend the shared model pool, so this adds no new trust
 * boundary. A deployment that hands out Studio accounts to untrusted users
 * should leave the limit in `.env` and treat the override as an operator tool.
 */

function fail(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: { "cache-control": "no-store" } });
}

function stateResponse(state: RateLimitState, status = 200): Response {
  return Response.json(
    { rateLimit: state },
    { status, headers: { "cache-control": "no-store" } },
  );
}

export async function GET(request: Request): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  loadRepoEnv();
  return stateResponse(readRateLimitState());
}

export async function PUT(request: Request): Promise<Response> {
  const gate = authorizeRequest(request);
  if (!gate.ok) return gate.response;

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return fail("Request body must be JSON.", 400);
  }

  const body = (payload ?? {}) as Record<string, unknown>;
  loadRepoEnv();

  // Reset: drop the override so the deployment default applies again.
  if (body.reset === true) {
    writeRateLimitOverride(null);
    return stateResponse(readRateLimitState());
  }

  if (typeof body.disabled !== "boolean") {
    return fail("Send { disabled: true }, { disabled: false, perMinute: n } or { reset: true }.", 400);
  }

  if (body.disabled) {
    writeRateLimitOverride(0);
    return stateResponse(readRateLimitState());
  }

  const perMinute = body.perMinute;
  if (typeof perMinute !== "number" || !Number.isInteger(perMinute)) {
    return fail("Re-enabling needs a whole number of generations per minute.", 400);
  }
  if (perMinute < 1) {
    return fail("Use { disabled: true } to switch the limit off, or a limit of at least 1.", 400);
  }
  if (perMinute > MAX_RATE_LIMIT_PER_MIN) {
    return fail(`That limit is above the maximum of ${MAX_RATE_LIMIT_PER_MIN} per minute.`, 413);
  }

  writeRateLimitOverride(perMinute);
  return stateResponse(readRateLimitState());
}
