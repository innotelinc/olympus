import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { createSign, generateKeyPairSync, type KeyObject } from "node:crypto";

/**
 * A local stand-in for Authentik: discovery document, JWKS, and a token
 * endpoint that mints real (RS256/ES256) signed id_tokens.
 *
 * Tests drive the actual Studio auth code against it over real HTTP, so the
 * only thing unverified is the provider itself — not our PKCE, state/nonce
 * handling, signature verification, or cookie logic.
 */

export type MockOidc = {
  issuer: string;
  /** The nonce the next id_token will carry. */
  setNonce: (nonce: string) => void;
  /** Merge overrides into the id_token claims (break iss/aud/exp to test rejection). */
  setClaims: (claims: Record<string, unknown>) => void;
  /** Sign with an EC key (exercises the JWS ES256 raw-signature path). */
  useEcKey: () => void;
  /** Sign with a key that is NOT in the published JWKS. */
  useUnknownSigningKey: () => void;
  /** Force a header alg, e.g. "none". */
  setAlgorithm: (alg: string | null) => void;
  /** Back to the published RS256 key with no overrides. */
  resetSigning: () => void;
  /** Whether the token endpoint should fail. */
  failTokenEndpoint: (status: number) => void;
  lastTokenRequest: () => { body: string; authorization: string | null } | null;
  counts: () => Record<"discovery" | "jwks" | "token", number>;
  close: () => Promise<void>;
};

function b64url(input: Buffer | string): string {
  return Buffer.from(input).toString("base64url");
}

function signJwt(
  header: Record<string, unknown>,
  claims: Record<string, unknown>,
  key: KeyObject,
  curve: boolean,
): string {
  const signingInput = `${b64url(JSON.stringify(header))}.${b64url(JSON.stringify(claims))}`;
  const signer = createSign(curve ? "SHA256" : "RSA-SHA256");
  signer.update(signingInput);
  // JWS ES* signatures are raw R||S, not DER.
  const signature = curve
    ? signer.sign({ key, dsaEncoding: "ieee-p1363" })
    : signer.sign(key);
  return `${signingInput}.${b64url(signature)}`;
}

export async function startMockOidc(options: {
  clientId: string;
  slug?: string;
}): Promise<MockOidc> {
  const slug = options.slug ?? "studio";

  const rsa = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const ec = generateKeyPairSync("ec", { namedCurve: "prime256v1" });
  const rogue = generateKeyPairSync("rsa", { modulusLength: 2048 });

  let useEc = false;
  let rogueSignature = false;
  let forcedAlgorithm: string | null = null;
  let tokenFailureStatus = 0;
  let nonce = "";
  let overrides: Record<string, unknown> = {};
  let lastToken: { body: string; authorization: string | null } | null = null;

  const counts = { discovery: 0, jwks: 0, token: 0 };

  const jwks = () => ({
    keys: [
      { ...rsa.publicKey.export({ format: "jwk" }), kid: "rsa-1", alg: "RS256", use: "sig" },
      { ...ec.publicKey.export({ format: "jwk" }), kid: "ec-1", alg: "ES256", use: "sig" },
    ],
  });

  const server = createServer((request, response) => {
    const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "127.0.0.1"}`);
    const origin = url.origin;
    // Authentik-style issuer: the application path is part of the issuer.
    const issuer = `${origin}/application/o/${slug}`;
    const send = (status: number, body: unknown) => {
      response.writeHead(status, { "content-type": "application/json" });
      response.end(JSON.stringify(body));
    };

    if (url.pathname === `/application/o/${slug}/.well-known/openid-configuration`) {
      counts.discovery += 1;
      return send(200, {
        issuer,
        authorization_endpoint: `${origin}/application/o/authorize/`,
        token_endpoint: `${origin}/application/o/token/`,
        jwks_uri: `${origin}/application/o/${slug}/jwks/`,
        userinfo_endpoint: `${origin}/application/o/userinfo/`,
        end_session_endpoint: `${origin}/application/o/${slug}/end-session/`,
        response_types_supported: ["code"],
        subject_types_supported: ["public"],
        id_token_signing_alg_values_supported: ["RS256", "ES256"],
        scopes_supported: ["openid", "email", "profile"],
        code_challenge_methods_supported: ["S256"],
      });
    }

    if (url.pathname === `/application/o/${slug}/jwks/`) {
      counts.jwks += 1;
      return send(200, jwks());
    }

    if (url.pathname === "/application/o/token/" && request.method === "POST") {
      counts.token += 1;

      let raw = "";
      request.on("data", (chunk) => {
        raw += chunk;
      });
      request.on("end", () => {
        lastToken = { body: raw, authorization: request.headers.authorization ?? null };

        if (tokenFailureStatus) {
          return send(tokenFailureStatus, { error: "invalid_grant" });
        }

        const now = Math.floor(Date.now() / 1000);
        const claims: Record<string, unknown> = {
          iss: issuer,
          aud: options.clientId,
          sub: "operator-123",
          email: "operator@example.com",
          name: "Test Operator",
          iat: now,
          nbf: now - 5,
          exp: now + 300,
          nonce,
          ...overrides,
        };

        const curve = forcedAlgorithm === "ES256" || (useEc && !forcedAlgorithm);
        const key = rogueSignature ? rogue.privateKey : curve ? ec.privateKey : rsa.privateKey;
        const header = {
          alg: forcedAlgorithm ?? (curve ? "ES256" : "RS256"),
          kid: rogueSignature ? "rogue-1" : curve ? "ec-1" : "rsa-1",
          typ: "JWT",
        };

        return send(200, {
          access_token: "mock-access-token",
          token_type: "Bearer",
          expires_in: 300,
          id_token: signJwt(header, claims, key, curve),
        });
      });
      return undefined;
    }

    return send(404, { error: "not_found", path: url.pathname });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;

  return {
    issuer: `http://127.0.0.1:${port}/application/o/${slug}`,
    setNonce: (value) => {
      nonce = value;
    },
    setClaims: (value) => {
      overrides = value;
    },
    useEcKey: () => {
      useEc = true;
    },
    useUnknownSigningKey: () => {
      rogueSignature = true;
    },
    setAlgorithm: (value) => {
      forcedAlgorithm = value;
    },
    resetSigning: () => {
      useEc = false;
      rogueSignature = false;
      forcedAlgorithm = null;
    },
    failTokenEndpoint: (status) => {
      tokenFailureStatus = status;
    },
    lastTokenRequest: () => lastToken,
    counts: () => ({ ...counts }),
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
}
