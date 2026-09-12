# Studio — the Olympus vibe-coding UI

Describe an app in plain language and it builds one: the model returns complete
source files, Studio parses them as they stream, and the result renders live in a
sandboxed frame. Iterate in plain language — the current files are sent with each
new instruction.

Next.js (App Router) app. It reads the same repo-root `.env` the rest of Olympus
uses and talks to the OmniRoute gateway server-side.

## How it works

```
browser ──POST /api/generate──▶ route handler ──▶ OmniRoute /chat/completions
   ▲                                                        │
   │◀──────── plain-text stream of source ◀── SSE frame ─────┘
   │
   ├─ parseFiles()           extract <file path="…"> blocks as they arrive
   └─ buildPreviewDocument() inline linked assets → sandboxed iframe
```

The gateway key lives only in the route handler. It is never sent to the browser
and never appears in the client bundle.

| Piece | File |
| --- | --- |
| Workspace UI (prompt, stream, tabs) | `components/Studio.tsx` |
| Sandboxed preview frame | `components/Preview.tsx` |
| File browser + copy | `components/CodeView.tsx` |
| Streaming gateway proxy | `app/api/generate/route.ts` |
| Gateway config, prompt contract, SSE parsing | `lib/omniroute.ts` |
| File-block parsing, asset inlining | `lib/files.ts` |
| Repo `.env` discovery | `lib/env.ts` |
| OIDC: PKCE, id_token verification, sessions | `lib/auth.ts` |
| Sign-in / callback / sign-out routes | `app/api/auth/{login,callback,logout}/route.ts` |
| Test suite + mock Authentik | `tests/` |
| Container image | `Dockerfile` |

## Run it

```bash
make studio-install    # once — npm ci in web/studio
make studio-dev        # http://localhost:3001
```

Production:

```bash
make studio-build      # next build
make studio           # next start
```

`STUDIO_STANDALONE=1` enables standalone output, which is what the container
image needs. `next start` does **not** support standalone and warns on every
boot, so it stays off for local runs — the Dockerfile sets it for the image
build only.

Studio needs a usable `OMNIROUTE_API_KEY`. It loads the repo-root `.env`
automatically (walking up from `web/studio`), so the same file the factory uses is
enough. Without a key, `/api/generate` answers `503` with a setup hint rather than
failing obscurely.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OMNIROUTE_BASE_URL` | `http://127.0.0.1:20128/v1` | Gateway root. Use `http://omniroute:20128/v1` from inside the compose network. |
| `OMNIROUTE_API_KEY` | — | Bearer key for the gateway. Required. |
| `OMNIROUTE_MODEL` | `auto/coding` | Model routed through the gateway. |
| `OMNIROUTE_CHAT_PATH` | `/chat/completions` | Override only if the gateway exposes the route elsewhere. |
| `STUDIO_PORT` | `3001` | Host port for the dev server. |
| `STUDIO_ACCESS_TOKEN` | — | When set, `/api/generate` requires an `x-studio-token` header. Empty = open. |
| `STUDIO_PUBLIC_HOST` | — | The hostname Cerulean points at the edge. |

Placeholders from `.env.example` (`change-me…`) count as unset, so an unedited
template fails loudly instead of sending a bogus key.

## The output contract

The system prompt (`lib/omniroute.ts`) requires the model to answer with nothing
but file blocks:

```
<file path="index.html">
…
</file>
```

- `index.html` is the entry point and must always be present.
- Files are rewritten in full each turn — never patches.
- Generated apps are vanilla HTML/CSS/JS only: no CDN tags, no imports, no
  external fonts, no third-party requests. The preview has no network access.

The parser tolerates streamed fragments — an unterminated block simply does not
match yet, so the preview only updates once a file is complete.

## Authentication — Authentik OIDC

Identity belongs to Authentik (TrustOps). When it is configured, every request
must carry a valid session; when it is not, Studio runs as a single-operator
tool. Nothing is stored server-side.

Set these (all present in `.env.example`) to switch it on:

| Variable | Notes |
| --- | --- |
| `OIDC_ISSUER_URL` | e.g. `https://auth.cerulean.innotel.us/application/o/studio/` |
| `OIDC_CLIENT_ID` | The Authentik application's client ID |
| `OIDC_CLIENT_SECRET` | Leave as the `change-me` placeholder and auth stays disabled |
| `OIDC_REDIRECT_URI` | Optional — derived from the request (honors `x-forwarded-proto`/`-host`) when unset |
| `OIDC_SCOPES` | Default `openid email profile` |
| `OIDC_ALLOWED_GROUPS` | Comma-separated group allow-list. Empty = any authenticated user |
| `STUDIO_SESSION_SECRET` | Signs the session + flow cookies. Empty = derived from the client secret |

Register the redirect URI in Authentik as `<host>/api/auth/callback`, and enable
the **Authorization Code + PKCE** flow.

What the implementation does:

- **Authorization Code + PKCE (S256)** — the verifier, `state`, and `nonce` travel
  in a short-lived signed cookie, so there is no server-side session store.
- **Signature verification.** The `id_token` is verified against the provider's
  JWKS (`RS256/384/512`, `PS256/384/512`, `ES256/384/512`), then its `iss`, `aud`,
  `exp`, `nbf`, and `nonce` claims are checked. `alg: none` is rejected.
- **Signed session cookie** — HMAC-SHA256, `HttpOnly`, `SameSite=Lax`, `Secure`
  behind TLS, 8-hour lifetime. Tampering fails closed.

### Authorization — restricting who gets in

Authentication is not authorization. `OIDC_ALLOWED_GROUPS` is a second gate, and
it is **opt-in**: leave it empty and any authenticated user may use Studio.

```bash
OIDC_ALLOWED_GROUPS=Cerulean,authentik Agent-Users
```

- Names are **comma-separated** and matched **exactly** (case-sensitive).
  Deliberately *not* whitespace-separated: Authentik really does name groups
  `authentik Agent-Users`, and splitting on spaces would quietly turn one group
  into two names that match nothing.
- No provider change is needed to enable it — Authentik releases `groups`
  through the `profile` scope, which is already in `OIDC_SCOPES`.
- A user outside the list is refused at the callback with **`403`** and is issued
  **no session at all**, so there is nothing to replay or elevate.
- The list is re-checked on **every request** against the groups carried in the
  signed cookie, so tightening it takes effect immediately rather than after the
  8-hour session expires.
- It fails closed: if the token has no `groups` claim, this is a denial, not a
  pass. Setting it to a group nobody is in locks everyone out — including you.

### Registering the application

Registering an application and provider changes your identity provider, so it is
a deliberate, explicit step: `scripts/authentik-studio-app.py` does it for you.
It takes an Authentik API token from `AUTHENTIK_TOKEN`, supports `--dry-run`,
and is idempotent — a re-run repairs an existing provider rather than
duplicating it. Two details it exists to get right, both learned against
Authentik 2026.8:

- **`grant_types` is not defaulted.** An omitted `grant_types` lands as `[]`,
  and `/authorize` then fails with `Invalid grant_type for provider`. The script
  sets `["authorization_code", "refresh_token"]` explicitly, and PATCHes an
  existing provider that is missing it.
- **The signing-key endpoint moved** from `/api/v3/core/certificatekeypairs/` to
  `/api/v3/crypto/certificatekeypairs/`. The script tries both, and treats an
  absent keypair as non-fatal.

Verified end to end against the live Authentik at `auth.cerulean.innotel.us`
(application `studio`, provider pk 25): the full authorization-code + PKCE
handshake completes, `id_token` verification passes, and a replayed code, a
tampered `state`, and a missing flow cookie are each rejected with `401`.

## Security posture

- **Sandboxed preview.** The frame runs with `allow-scripts` but *not*
  `allow-same-origin`, so generated code gets a unique origin and cannot reach
  Studio's DOM, cookies, or storage. Requests are sent with
  `referrerPolicy="no-referrer"`.
- **Server-side credentials.** The gateway key never reaches the browser.
- **Bounded input.** Prompt length, prior-file count, and per-file size are
  capped in the route handler; upstream error text is truncated before it is
  echoed back.
- **Identity.** Authentik OIDC when configured (see above); otherwise the
  optional `STUDIO_ACCESS_TOKEN` shared gate protects the generate route.

## Running in a container

The image is a multi-stage standalone build (verified: non-root `nextjs` user,
~230 MB, healthcheck reaching `healthy`).

```bash
make docker-studio        # build the image
docker compose up -d studio
```

### Connecting it to the gateway

This is the one thing that bites. `host.docker.internal` only reaches a gateway
that is bound to a **routable** interface. A gateway published as
`127.0.0.1:20128` is loopback-only and is *not* reachable from a bridge network.
Pick the wiring that matches your topology — the first two were verified end to
end from inside the container:

| Topology | `OMNIROUTE_BASE_URL` |
| --- | --- |
| Gateway is a sibling container | `http://omniroute:20128/v1` — and attach Studio to that container's network |
| Gateway is loopback-published on the host | Run Studio with host networking, then `http://127.0.0.1:20128/v1` |
| Gateway is on another machine | `http://<lan-ip>:20128/v1` |
| Gateway is published on `0.0.0.0` | `http://host.docker.internal:20128/v1` (the compose default) |

## Deploying behind Cerulean + NPM Edge

Cerulean owns DNS and TLS and NPM Edge fronts the host; point
`STUDIO_PUBLIC_HOST` at the Studio service and let the edge terminate TLS. Studio
emits a standalone build, so it needs no Node toolchain on the host.

The `studio` compose service is wired up. Because Studio derives the callback
from the request when `OIDC_REDIRECT_URI` is unset, the hostname the edge
forwards (`x-forwarded-host`/`-proto`) determines the `redirect_uri` — so the
public URL must be one of the provider's registered redirect URIs.

Both stacks label their containers `autoheal=true` and run an `autoheal`
service, so a container that fails its own healthcheck is restarted rather than
sitting wedged. Docker never restarts an unhealthy container on its own.

## Testing

```bash
make studio-test      # or: cd web/studio && npm test
```

110 tests across five files, no network required:

| File | Covers |
| --- | --- |
| `tests/files.test.ts` | Parsing, streamed fragments, duplicate paths, asset inlining, orphaned assets, no-HTML fallback, traversal, listing escaping |
| `tests/omniroute.test.ts` | Config defaults, placeholder keys, the prompt contract, SSE parsing (both API shapes, split frames, `[DONE]`, malformed frames) |
| `tests/env.test.ts` | Repo `.env` discovery, nearest-file precedence, boundary stop, quoting, malformed lines, idempotence |
| `tests/route.test.ts` | Every `/api/generate` path: 400/413/503/401/502, auth gating, streaming, bearer header, prior-file forwarding |
| `tests/auth.test.ts` | Full OIDC flow against a mock Authentik — PKCE, state, nonce, JWKS signature verification (RS256 + ES256), claim rejection, session cookies, login/callback routes, and the group policy (allow, deny, no-`groups` claim, per-request re-check) |

The mock provider (`tests/helpers/mock-oidc.ts`) serves a real discovery
document, JWKS, and token endpoint over localhost and mints genuinely signed
tokens, so the suite runs with no network. The same flow has also been driven
against the live Authentik — see Verification below.

### Integration run (opt-in)

The mock proves our code, not the provider's. `tests/integration/` closes that
gap by driving a real Authentik, and is kept out of `npm test` entirely so the
default run can never start signing in to a live IdP:

```bash
cd web/studio
STUDIO_E2E_BASE_URL=http://localhost:3001 \
STUDIO_E2E_AUTHENTIK_URL=https://auth.cerulean.innotel.us \
STUDIO_E2E_USERNAME=... STUDIO_E2E_PASSWORD=... \
STUDIO_E2E_INSECURE=1 STUDIO_E2E_GENERATE=1 \
  npm run test:integration
```

It performs a real sign-in (so the account's last-login moves) and asserts the
handshake, the session, and the negative paths — replayed code, tampered `state`,
missing flow cookie, anonymous generate. Set `STUDIO_E2E_EXPECT_DENIED=1` when
Studio's `OIDC_ALLOWED_GROUPS` excludes the account and the same run asserts the
`403` lockdown instead. Without the opt-in variables every test skips.

CI runs it from the `authentik-e2e` job, off unless the repository variable
`AUTHENTIK_E2E` is `true` — a LAN-only provider needs a self-hosted runner.

The suite is hermetic: `route.test.ts` and `auth.test.ts` stub the repo-`.env`
loader rather than inheriting it. `delete process.env.KEY` is *not* isolation —
the loader reads the key straight back off disk — so without the stub a checkout
with real OIDC credentials turned the auth gate on before the assertions under
test ran. It passes both on a fully configured `.env` and with no `.env` at all.

## Verification

Checked on the current tree:

- `npx tsc --noEmit` — clean. `next build` — clean, no tracer warnings.
- `npm test` — 100 passing.
- `GET /` — `200`; `/api/generate` — `400` empty prompt, `400` malformed body,
  `413` oversized prompt, `503` no gateway key, `401` unauthenticated/unauthorized.
- **Full OIDC flow against live Authentik**, driven both by the local server and
  by the container image: login → code → token exchange → `id_token`
  verification → session cookie → authenticated page. Negative cases behaved:
  replayed code `401` (`invalid_grant`), tampered `state` `401`, missing flow
  cookie `401`, unauthenticated `/api/generate` `401`.
- **Authenticated generate through the container** — `200`, streaming a real
  `<file path="index.html">` block back from the gateway.
- **Group policy, verified against the live provider in both directions.** With
  `OIDC_ALLOWED_GROUPS=Cerulean` (a group the test account is in) the sign-in
  completes and the app renders. With the allow-list switched to a group it is
  *not* in, the callback answers `403` and issues **no** session.
- The live handshake is now a repeatable test — `npm run test:integration`, 8
  assertions — instead of a one-off script.
- Container image: builds, runs as non-root `nextjs` (uid 1001), ~230 MB, PID 1
  is `next-server`, healthcheck reaches `healthy`.
- The `autoheal` service was verified against a deliberately unhealthy labelled
  container: it detected the failure and restarted it, and touched nothing else.
- The container image builds, runs as the non-root `nextjs` user (~230 MB), and
  its healthcheck reaches `healthy`. Generating through the container against the
  gateway returned a real app — verified both on a shared network and with host
  networking.
- The OIDC flow was exercised end to end against the mock provider: sign-in
  redirect with PKCE, code exchange carrying the verifier and client auth, and a
  session cookie that then unlocks `/api/generate`.

Not verified: a handshake against a live Authentik instance (none has an OIDC
application registered for Studio yet).
