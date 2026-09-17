import { authorizeAdmin } from "@/lib/auth";
import { collectStatus } from "@/lib/admin";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * The admin panel's data.
 *
 * One route rather than one per card, because everything here is read in a single
 * pass to answer a single question — "is this deployment in a state where the next
 * build will work?" — and splitting it into six endpoints would make the panel
 * render six moments instead of one.
 *
 * `authorizeAdmin`, not `authorizeRequest`: signing in is not the privilege being
 * asked for. It is the first gate too (a caller with no session is told to sign in
 * rather than forbidden), so the two failures stay distinguishable.
 */
export async function GET(request: Request): Promise<Response> {
  const gate = authorizeAdmin(request);
  if (!gate.ok) return gate.response;

  const status = await collectStatus(gate.session);

  return Response.json(status, {
    status: 200,
    headers: {
      // Deployment state: one viewer's copy must never be reused for another's.
      "cache-control": "no-store",
    },
  });
}
