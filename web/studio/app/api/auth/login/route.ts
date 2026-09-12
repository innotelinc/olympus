import {
  FLOW_COOKIE,
  FLOW_TTL_SECONDS,
  buildAuthorizeUrl,
  createPkce,
  discover,
  isSecureRequestOrigin,
  randomToken,
  readAuthConfig,
  resolveRedirectUri,
  serializeCookie,
  signFlow,
} from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: Request): Promise<Response> {
  const config = readAuthConfig();

  // Auth disabled — nothing to sign in to.
  if (!config) {
    return new Response(null, { status: 307, headers: { location: "/", "cache-control": "no-store" } });
  }

  let document;
  try {
    document = await discover(config);
  } catch (error) {
    const detail = error instanceof Error ? error.message : "unknown error";
    return Response.json({ error: `Cannot reach the identity provider. ${detail}` }, { status: 502 });
  }

  const { verifier, challenge } = createPkce();
  const flow = { state: randomToken(), nonce: randomToken(), verifier };
  const redirectUri = resolveRedirectUri(config, request);

  const location = buildAuthorizeUrl(config, document, { ...flow, challenge, redirectUri });
  const secure = isSecureRequestOrigin(request);

  const headers = new Headers({
    location,
    "cache-control": "no-store",
  });
  headers.append(
    "set-cookie",
    serializeCookie(FLOW_COOKIE, signFlow(flow, config), {
      maxAge: FLOW_TTL_SECONDS,
      secure,
    }),
  );

  return new Response(null, { status: 307, headers });
}
