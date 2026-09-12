import {
  FLOW_COOKIE,
  GroupNotAllowedError,
  SESSION_COOKIE,
  SESSION_TTL_SECONDS,
  clearCookie,
  exchangeCode,
  isAuthorized,
  isSecureRequestOrigin,
  readAuthConfig,
  readFlow,
  resolveRedirectUri,
  serializeCookie,
  sessionFromClaims,
  signSession,
  verifyIdToken,
} from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function deny(message: string, status = 400): Response {
  return new Response(message, {
    status,
    headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" },
  });
}

export async function GET(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const config = readAuthConfig();

  if (!config) {
    return new Response(null, { status: 307, headers: { location: "/", "cache-control": "no-store" } });
  }

  const providerError = url.searchParams.get("error");
  if (providerError) {
    const description = url.searchParams.get("error_description") ?? "";
    return deny(`Sign-in was rejected by the identity provider: ${providerError} ${description}`.trim(), 401);
  }

  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  if (!code || !state) return deny("Callback is missing the authorization code or state.");

  const cookieHeader = request.headers.get("cookie");
  const flow = readFlow(cookieHeader, config);
  if (!flow) return deny("Sign-in flow expired or the flow cookie is missing. Start again.", 401);
  if (state !== flow.state) return deny("State mismatch — possible cross-site request. Start again.", 401);

  let session;
  try {
    const tokens = await exchangeCode(config, code, flow.verifier, resolveRedirectUri(config, request));
    if (!tokens.id_token) throw new Error("Token response contained no id_token");

    const claims = await verifyIdToken(tokens.id_token, config, flow.nonce);
    session = sessionFromClaims(claims);

    // Authenticated is not authorized. OIDC_ALLOWED_GROUPS is a second gate and
    // it must refuse before a session cookie is ever issued — otherwise a
    // disallowed user would hold a valid session and every route would have to
    // re-derive the decision.
    if (!isAuthorized(session, config)) {
      throw new GroupNotAllowedError("not in an allowed group");
    }
  } catch (error) {
    if (error instanceof GroupNotAllowedError) {
      // 403, not 401: the credentials were fine, this account simply has no
      // group in OIDC_ALLOWED_GROUPS.
      return deny(
        "You signed in, but your account is not in a group allowed to use Studio. " +
          "Ask an administrator to add you to a permitted group.",
        403,
      );
    }
    const detail = error instanceof Error ? error.message : "unknown error";
    return deny(`Sign-in failed: ${detail}`, 401);
  }

  const secure = isSecureRequestOrigin(request);
  const headers = new Headers({ location: "/", "cache-control": "no-store" });
  headers.append("set-cookie", clearCookie(FLOW_COOKIE));
  headers.append(
    "set-cookie",
    serializeCookie(SESSION_COOKIE, signSession(session, config), {
      maxAge: SESSION_TTL_SECONDS,
      secure,
    }),
  );

  return new Response(null, { status: 307, headers });
}
