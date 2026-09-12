import {
  constants,
  createHash,
  createHmac,
  createPublicKey,
  createVerify,
  randomBytes,
  timingSafeEqual,
  // Node's JsonWebKey (index-signature) rather than the DOM global of the same name.
  type JsonWebKey as CryptoJsonWebKey,
} from "node:crypto";
import { loadRepoEnv } from "./env";
import { isPlaceholderSecret } from "./omniroute";

/**
 * Dependency-free OIDC authorization-code + PKCE for Studio.
 *
 * The identity provider is Authentik (TrustOps owns identity). Nothing here
 * touches the filesystem or a session store: the flow state travels in a
 * short-lived signed cookie and the session is a signed cookie, so the app
 * stays stateless behind the edge.
 *
 * When OIDC is not configured (no issuer, or placeholder credentials), auth is
 * disabled entirely and Studio behaves as a single-operator tool.
 */

export const SESSION_COOKIE = "studio_session";
export const FLOW_COOKIE = "studio_oidc_flow";

export const SESSION_TTL_SECONDS = 8 * 60 * 60;
export const FLOW_TTL_SECONDS = 10 * 60;
const DISCOVERY_TTL_MS = 10 * 60 * 1000;
const JWKS_TTL_MS = 10 * 60 * 1000;

export type AuthConfig = {
  /** Issuer exactly as configured — this is what the `iss` claim is matched against. */
  issuer: string;
  clientId: string;
  clientSecret: string;
  /** Empty means "derive from the incoming request". */
  redirectUri: string;
  scopes: string;
  sessionSecret: string;
  /**
   * Group allow-list (comma-separated, exact match). Empty means "any
   * authenticated user", so the policy is strictly opt-in and an unconfigured
   * deployment cannot lock itself out.
   */
  allowedGroups: string[];
};

export type Session = {
  sub: string;
  email?: string;
  name?: string;
  /**
   * Carried in the signed cookie so the allow-list can be re-checked on every
   * request. Tightening OIDC_ALLOWED_GROUPS then takes effect immediately
   * instead of waiting up to 8 hours for outstanding sessions to expire.
   */
  groups?: string[];
};

export type FlowState = {
  state: string;
  nonce: string;
  verifier: string;
};

type DiscoveryDocument = {
  issuer?: string;
  authorization_endpoint: string;
  token_endpoint: string;
  jwks_uri: string;
  end_session_endpoint?: string;
};

interface Jwk {
  [key: string]: unknown;
  kid?: string;
  alg?: string;
  use?: string;
}

type JwtHeader = { alg?: string; kid?: string; typ?: string };

type JwtClaims = {
  iss?: string;
  aud?: string | string[];
  exp?: number;
  nbf?: number;
  iat?: number;
  nonce?: string;
  sub?: string;
  email?: string;
  name?: string;
  preferred_username?: string;
  /** Authentik sends a JSON array in `profile` and a delimited string in `groups`. */
  groups?: string[] | string;
};

const ALGORITHMS: Record<string, { hash: string; curve: boolean; pss?: boolean }> = {
  RS256: { hash: "RSA-SHA256", curve: false },
  RS384: { hash: "RSA-SHA384", curve: false },
  RS512: { hash: "RSA-SHA512", curve: false },
  PS256: { hash: "RSA-SHA256", curve: false, pss: true },
  PS384: { hash: "RSA-SHA384", curve: false, pss: true },
  PS512: { hash: "RSA-SHA512", curve: false, pss: true },
  ES256: { hash: "SHA256", curve: true },
  ES384: { hash: "SHA384", curve: true },
  ES512: { hash: "SHA512", curve: true },
};

/* ---- encoding helpers --------------------------------------------------- */

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

function fromB64url(value: string): Buffer {
  return Buffer.from(value, "base64url");
}

/** JWS ES* signatures are raw R||S; node wants DER. */
function rawEcdsaToDer(raw: Buffer): Buffer {
  const half = Math.floor(raw.length / 2);
  const encodeInt = (slice: Buffer): Buffer => {
    let start = 0;
    while (start < slice.length - 1 && slice[start] === 0) start += 1;
    let body = slice.subarray(start);
    if ((body[0] ?? 0) & 0x80) body = Buffer.concat([Buffer.from([0]), body]);
    return Buffer.concat([Buffer.from([0x02, body.length]), body]);
  };
  const r = encodeInt(raw.subarray(0, half));
  const s = encodeInt(raw.subarray(half));
  const inner = Buffer.concat([r, s]);
  return Buffer.concat([Buffer.from([0x30, inner.length]), inner]);
}

/* ---- configuration ------------------------------------------------------ */

export function normalizeIssuer(issuer: string): string {
  return issuer.replace(/\/+$/, "");
}

export function readAuthConfig(): AuthConfig | null {
  loadRepoEnv();

  const issuer = process.env.OIDC_ISSUER_URL?.trim() ?? "";
  const clientId = process.env.OIDC_CLIENT_ID?.trim() ?? "";
  const clientSecret = process.env.OIDC_CLIENT_SECRET?.trim() ?? "";

  if (!issuer || isPlaceholderSecret(clientId) || isPlaceholderSecret(clientSecret)) return null;

  const configuredSecret = process.env.STUDIO_SESSION_SECRET?.trim() ?? "";
  const sessionSecret =
    configuredSecret && !isPlaceholderSecret(configuredSecret)
      ? configuredSecret
      : createHash("sha256").update(`studio-session:${clientSecret}`).digest("hex");

  return {
    issuer,
    clientId,
    clientSecret,
    redirectUri: process.env.OIDC_REDIRECT_URI?.trim() ?? "",
    scopes: process.env.OIDC_SCOPES?.trim() || "openid email profile",
    sessionSecret,
    allowedGroups: parseGroupList(process.env.OIDC_ALLOWED_GROUPS),
  };
}

/* ---- authorization policy ---------------------------------------------- */

/** Thrown when a user authenticated but is not in an allowed group. */
export class GroupNotAllowedError extends Error {}

/**
 * Normalize a `groups` claim to a list.
 *
 * Authentik exposes groups as a JSON array from the `profile` scope mapping and
 * as a space-delimited string from its dedicated `groups` mapping, so both
 * shapes are accepted rather than assuming one.
 */
export function normalizeGroups(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value
      .filter((entry): entry is string => typeof entry === "string")
      .map((entry) => entry.trim())
      .filter(Boolean);
  }

  if (typeof value === "string") {
    return value
      .split(/[\s,]+/)
      .map((entry) => entry.trim())
      .filter(Boolean);
  }

  return [];
}

/**
 * Parse the configured allow-list. **Comma-separated only**, matched exactly.
 *
 * Unlike the claim, this must not split on whitespace: Authentik group names
 * routinely contain spaces ("authentik Agent-Users"), so whitespace splitting
 * would quietly turn one group into two names that match nothing — a fail-closed
 * lockout that looks like a policy that simply does not apply.
 */
export function parseGroupList(value: string | undefined): string[] {
  if (!value) return [];
  return value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
}

/**
 * Whether this session may use Studio.
 *
 * Fails closed: when the allow-list is non-empty and the session carries no
 * matching group (including a token with no `groups` claim at all), access is
 * denied. An empty allow-list permits any authenticated user.
 */
export function isAuthorized(session: Session, config: AuthConfig): boolean {
  if (config.allowedGroups.length === 0) return true;
  return (session.groups ?? []).some((group) => config.allowedGroups.includes(group));
}

export function discoveryUrl(issuer: string): string {
  return `${normalizeIssuer(issuer)}/.well-known/openid-configuration`;
}

/** Derive the callback URL from the request, honoring the edge's forwarded proto. */
export function resolveRedirectUri(config: AuthConfig, request: Request): string {
  if (config.redirectUri) return config.redirectUri;
  const url = new URL(request.url);
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  const proto = forwardedProto || url.protocol.replace(":", "");
  const host = request.headers.get("x-forwarded-host")?.split(",")[0]?.trim() || url.host;
  return `${proto}://${host}/api/auth/callback`;
}

/** True when the request reached us over TLS (directly or via the edge). */
export function isSecureRequestOrigin(request: Request): boolean {
  const forwardedProto = request.headers.get("x-forwarded-proto")?.split(",")[0]?.trim();
  return (forwardedProto || new URL(request.url).protocol.replace(":", "")) === "https";
}

/* ---- request gate ------------------------------------------------------- */

/**
 * The single access gate for every Studio route.
 *
 * Both checks live here rather than in each handler: identity first (when OIDC
 * is configured, only a signed-in, allowed user gets through), then the
 * optional shared `STUDIO_ACCESS_TOKEN` for deployments without an IdP. A new
 * route that forgets one of them would silently widen access — so routes ask
 * this, and there is one place to audit.
 */
export type GateResult =
  | { ok: true; session: Session | null }
  | { ok: false; response: Response };

function deny(message: string, status: number): Response {
  return Response.json({ error: message }, { status, headers: { "cache-control": "no-store" } });
}

export function authorizeRequest(request: Request): GateResult {
  const config = readAuthConfig();
  let session: Session | null = null;

  if (config) {
    session = readSession(request.headers.get("cookie"), config);
    if (!session) return { ok: false, response: deny("Sign in to use Studio.", 401) };

    // Re-checked per request from the session's own groups, so tightening
    // OIDC_ALLOWED_GROUPS applies at once rather than at session expiry.
    if (!isAuthorized(session, config)) {
      return {
        ok: false,
        response: deny("Your account is not in a group allowed to use Studio.", 403),
      };
    }
  }

  // Optional shared gate. Set STUDIO_ACCESS_TOKEN to require a header; leave it
  // unset on a trusted network or behind an identity-aware proxy.
  loadRepoEnv();
  const accessToken = process.env.STUDIO_ACCESS_TOKEN?.trim();
  if (accessToken && request.headers.get("x-studio-token") !== accessToken) {
    return {
      ok: false,
      response: deny("Studio requires an access token. Add it under Settings.", 401),
    };
  }

  return { ok: true, session };
}

/* ---- discovery + jwks --------------------------------------------------- */

let discoveryCache: { url: string; document: DiscoveryDocument; at: number } | null = null;
let jwksCache: { url: string; keys: Jwk[]; at: number } | null = null;

export async function discover(config: AuthConfig): Promise<DiscoveryDocument> {
  const url = discoveryUrl(config.issuer);

  if (discoveryCache && discoveryCache.url === url && Date.now() - discoveryCache.at < DISCOVERY_TTL_MS) {
    return discoveryCache.document;
  }

  const response = await fetch(url, { headers: { accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`OIDC discovery failed: ${url} returned ${response.status}`);
  }

  const document = (await response.json()) as DiscoveryDocument;
  if (!document.authorization_endpoint || !document.token_endpoint || !document.jwks_uri) {
    throw new Error(`OIDC discovery at ${url} is missing required endpoints`);
  }

  discoveryCache = { url, document, at: Date.now() };
  return document;
}

async function fetchJwks(document: DiscoveryDocument): Promise<Jwk[]> {
  if (jwksCache && jwksCache.url === document.jwks_uri && Date.now() - jwksCache.at < JWKS_TTL_MS) {
    return jwksCache.keys;
  }

  const response = await fetch(document.jwks_uri, { headers: { accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`JWKS fetch failed: ${document.jwks_uri} returned ${response.status}`);
  }

  const body = (await response.json()) as { keys?: unknown };
  const keys = Array.isArray(body.keys) ? (body.keys as Jwk[]) : [];
  jwksCache = { url: document.jwks_uri, keys, at: Date.now() };
  return keys;
}

/* ---- cookies ------------------------------------------------------------ */

export function serializeCookie(
  name: string,
  value: string,
  options: { maxAge: number; secure: boolean },
): string {
  const parts = [
    `${name}=${value}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${options.maxAge}`,
  ];
  if (options.secure) parts.push("Secure");
  return parts.join("; ");
}

export function clearCookie(name: string): string {
  return `${name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0`;
}

export function readCookie(header: string | null, name: string): string | undefined {
  if (!header) return undefined;
  for (const part of header.split(";")) {
    const separator = part.indexOf("=");
    if (separator === -1) continue;
    if (part.slice(0, separator).trim() === name) {
      return part.slice(separator + 1).trim();
    }
  }
  return undefined;
}

/* ---- signed payloads ---------------------------------------------------- */

function sign(payload: Record<string, unknown>, secret: string, ttlSeconds: number): string {
  const body = { ...payload, exp: Math.floor(Date.now() / 1000) + ttlSeconds };
  const encoded = b64url(JSON.stringify(body));
  const signature = createHmac("sha256", secret).update(encoded).digest("base64url");
  return `${encoded}.${signature}`;
}

function unsign<T>(token: string | undefined, secret: string): T | null {
  if (!token) return null;

  const separator = token.lastIndexOf(".");
  if (separator <= 0) return null;

  const encoded = token.slice(0, separator);
  const provided = Buffer.from(token.slice(separator + 1));
  const expected = Buffer.from(createHmac("sha256", secret).update(encoded).digest("base64url"));

  if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) return null;

  try {
    const body = JSON.parse(fromB64url(encoded).toString("utf8")) as T & { exp?: number };
    if (typeof body.exp === "number" && body.exp < Math.floor(Date.now() / 1000)) return null;
    return body;
  } catch {
    return null;
  }
}

export function signSession(session: Session, config: AuthConfig): string {
  return sign({ ...session }, config.sessionSecret, SESSION_TTL_SECONDS);
}

export function readSession(cookieHeader: string | null, config: AuthConfig): Session | null {
  const payload = unsign<Session>(readCookie(cookieHeader, SESSION_COOKIE), config.sessionSecret);
  return payload && typeof payload.sub === "string" ? payload : null;
}

export function signFlow(flow: FlowState, config: AuthConfig): string {
  return sign({ ...flow }, config.sessionSecret, FLOW_TTL_SECONDS);
}

export function readFlow(cookieHeader: string | null, config: AuthConfig): FlowState | null {
  const payload = unsign<FlowState>(readCookie(cookieHeader, FLOW_COOKIE), config.sessionSecret);
  return payload && payload.state && payload.nonce && payload.verifier ? payload : null;
}

/* ---- the flow ----------------------------------------------------------- */

export function createPkce(): { verifier: string; challenge: string } {
  const verifier = b64url(randomBytes(32));
  return { verifier, challenge: createHash("sha256").update(verifier).digest("base64url") };
}

export function randomToken(bytes = 16): string {
  return b64url(randomBytes(bytes));
}

export function buildAuthorizeUrl(
  config: AuthConfig,
  document: DiscoveryDocument,
  input: FlowState & { challenge: string; redirectUri: string },
): string {
  const url = new URL(document.authorization_endpoint);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", config.clientId);
  url.searchParams.set("redirect_uri", input.redirectUri);
  url.searchParams.set("scope", config.scopes);
  url.searchParams.set("state", input.state);
  url.searchParams.set("nonce", input.nonce);
  url.searchParams.set("code_challenge", input.challenge);
  url.searchParams.set("code_challenge_method", "S256");
  return url.toString();
}

export type TokenSet = { id_token?: string; access_token?: string; expires_in?: number };

export async function exchangeCode(
  config: AuthConfig,
  code: string,
  verifier: string,
  redirectUri: string,
): Promise<TokenSet> {
  const document = await discover(config);

  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: redirectUri,
    client_id: config.clientId,
    code_verifier: verifier,
  });

  const headers: Record<string, string> = {
    "content-type": "application/x-www-form-urlencoded",
    accept: "application/json",
  };
  headers.authorization = `Basic ${Buffer.from(`${config.clientId}:${config.clientSecret}`).toString("base64")}`;

  const response = await fetch(document.token_endpoint, { method: "POST", headers, body });
  if (!response.ok) {
    // Bounded: never echo the submitted credentials back.
    const detail = (await response.text()).slice(0, 300);
    throw new Error(`Token endpoint returned ${response.status}${detail ? ` — ${detail}` : ""}`);
  }

  return (await response.json()) as TokenSet;
}

export async function verifyIdToken(
  idToken: string,
  config: AuthConfig,
  expectedNonce: string,
): Promise<JwtClaims> {
  const parts = idToken.split(".");
  if (parts.length !== 3) throw new Error("id_token is not a JWS");

  const [headerPart, payloadPart, signaturePart] = parts;
  const header = JSON.parse(fromB64url(headerPart).toString("utf8")) as JwtHeader;
  const claims = JSON.parse(fromB64url(payloadPart).toString("utf8")) as JwtClaims;

  const algorithm = header.alg ?? "";
  const spec = ALGORITHMS[algorithm];
  if (!spec) throw new Error(`Unsupported id_token algorithm: ${algorithm || "(absent)"}`);

  const document = await discover(config);
  const keys = await fetchJwks(document);
  const jwk = header.kid ? keys.find((key) => key.kid === header.kid) : keys[0];
  if (!jwk) throw new Error(`No JWKS entry matches kid ${header.kid ?? "(absent)"}`);

  const publicKey = createPublicKey({ key: jwk as unknown as CryptoJsonWebKey, format: "jwk" });
  const verifier = createVerify(spec.hash);
  verifier.update(`${headerPart}.${payloadPart}`);

  const provided = fromB64url(signaturePart);
  const signed = spec.curve ? rawEcdsaToDer(provided) : provided;

  const verified = spec.pss
    ? verifier.verify(
        {
          key: publicKey,
          padding: constants.RSA_PKCS1_PSS_PADDING,
          saltLength: constants.RSA_PSS_SALTLEN_DIGEST,
        },
        signed,
      )
    : verifier.verify(publicKey, signed);

  if (!verified) throw new Error("id_token signature did not verify");

  const now = Math.floor(Date.now() / 1000);

  if (!claims.iss || normalizeIssuer(claims.iss) !== normalizeIssuer(config.issuer)) {
    throw new Error(`id_token issuer mismatch: ${claims.iss ?? "(absent)"}`);
  }

  const audiences = Array.isArray(claims.aud) ? claims.aud : claims.aud ? [claims.aud] : [];
  if (!audiences.includes(config.clientId)) {
    throw new Error("id_token audience does not include this client");
  }

  if (typeof claims.exp === "number" && claims.exp < now - 30) throw new Error("id_token is expired");
  if (typeof claims.nbf === "number" && claims.nbf > now + 30) throw new Error("id_token is not yet valid");
  if (!claims.nonce || claims.nonce !== expectedNonce) throw new Error("id_token nonce mismatch");

  return claims;
}

export function sessionFromClaims(claims: JwtClaims): Session {
  return {
    sub: claims.sub ?? "unknown",
    email: claims.email,
    name: claims.name ?? claims.preferred_username,
    groups: normalizeGroups(claims.groups),
  };
}
