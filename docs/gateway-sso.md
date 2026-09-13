# Putting the gateway dashboard behind Cerulean Authentik

The OmniRoute dashboard manages provider credentials and can read them, so it
should not be reachable on a shared password. It is now fronted by an
identity-aware proxy that authenticates against Cerulean Authentik and admits
only members of `cerulean-platform`:

```
browser ──https──▶ edge (NPM, another host)
                       │  gateway.olympus.innotel.us
                       ▼  http://192.168.1.10:20129
                 gateway-sso (oauth2-proxy) ──▶ Authentik OIDC + group check
                       │  http://127.0.0.1:20128
                       ▼
                    omniroute (dashboard, still loopback-only)
```

The proxy is the front door; the gateway is unchanged and still published on the
host's loopback only. OmniRoute's **own** OIDC stays disabled, for the reason in
the next section — that is not a preference, it is the one path that cannot work.

| | |
| --- | --- |
| Proxy | `olympus-gateway-sso` (`quay.io/oauth2-proxy/oauth2-proxy:v7.7.1`) |
| Compose | `compose.gateway-sso.yml`, host networking, listens `0.0.0.0:20129` |
| Upstream | `http://127.0.0.1:20128` (the gateway's loopback binding) |
| Authentik application | `OmniRoute Gateway` (slug `omniroute`) |
| Client id | `omniroute` |
| Issuer | `https://auth.cerulean.innotel.us/application/o/omniroute/` |
| Callback in use | `https://gateway.olympus.innotel.us/oauth2/callback` |
| Access rule | any verified email clears OIDC; only group `cerulean-platform` gets in |

## Why not OmniRoute's own OIDC

OmniRoute's OIDC support is real — settings, login route, callback, JWKS
verification with `jose`, an allow-list — but its issuer handling makes it
incompatible with **any** IdP whose `iss` ends in a slash, which Authentik always
does.

The callback strips the trailing slash from the configured issuer and then
requires an exact match on that stripped value:

```ts
// /app/src/app/api/auth/oidc/callback/route.ts:73
const issuer = settings.oidcIssuer.trim().replace(/\/$/, "");
// :162
const { payload } = await jwtVerify(idToken, JWKS, { issuer, audience: clientId });
```

Authentik builds `iss` from the same function that publishes its discovery
document (`authentik/providers/oauth2/models.py: get_issuer` →
`request.build_absolute_uri(reverse("...provider-root"))`, a path that ends in
`/`). Measured against this deployment:

```
  configured issuer  : https://auth.cerulean.innotel.us/application/o/omniroute/
  expected (stripped): https://auth.cerulean.innotel.us/application/o/omniroute
  Authentik iss      : https://auth.cerulean.innotel.us/application/o/omniroute/
  match              : false
```

`jose` compares with `.includes()` on an exact value — no normalisation — so the
check fails and the browser lands on `/login?oidc_error=id_token_invalid`. The
password fallback then still works, which is why this is a dead end rather than a
lockout.

**No setting avoids it**, and upgrading does not help: the same two lines are on
`diegosouzapw/OmniRoute@main`, checked 2026-09-13. The strip is unconditional, so
a configured issuer only ever *loses* a slash; the one string that would survive
(`…/omniroute//`) makes the discovery and authorize URLs double-slashed, and this
edge does not merge slashes. Authentik's other issuer mode (`global`) also ends in
a slash, so it fails the same way while additionally weakening the per-application
binding.

The one-line fix is real and worth upstreaming — `jose` accepts a list of
acceptable issuers:

```diff
-    const { payload } = await jwtVerify(idToken, JWKS, { issuer, audience: clientId });
+    const { payload } = await jwtVerify(idToken, JWKS, {
+      // Authentik's `iss` always ends with a slash; the value above is stripped
+      // for URL building, so accept both forms.
+      issuer: [issuer, `${issuer}/`],
+      audience: clientId,
+    });
```

The `/api/auth/oidc/callback` URI is registered on the provider anyway, so
enabling `oidcEnabled` needs no new Authentik work the day that lands. Until
then it is inert.

## Setting it up

```bash
make gateway-oidc ARGS=--rotate-secret   # register/repair the client; prints the secret ONCE
# put it in .env as GATEWAY_OIDC_CLIENT_SECRET, and set GATEWAY_SSO_COOKIE_SECRET:
#   openssl rand -base64 32
make gateway-sso-up
make gateway-sso-check
```

`--rotate-secret` exists because Authentik stores client secrets write-only: a
provider that already exists has no readable value, so rotation is the only way
to obtain one. The previous secret stops working the moment it runs. An ordinary
re-run — which repairs `grant_types` and missing redirect URIs — never touches
the secret, and `scripts/tests/test_authentik_app.py` pins both behaviours.

For a fresh deployment, `make gateway-oidc` also registers the local dev callback
and derives both callbacks from `GATEWAY_PUBLIC_HOST`, so moving the host is a
one-line `.env` change plus a re-run.

## What was verified, and what was not

Verified against the live deployment:

| Check | Result |
| --- | --- |
| `make gateway-sso-check` | ok — live on `127.0.0.1:20129`, `/` redirects to `…/application/o/authorize/` as client `omniroute` |
| Proxy on the LAN | `/ping` → `200` from `192.168.1.10:20129` as well as loopback |
| Unauthenticated `/` and `/dashboard` | `302` to Authentik with `redirect_uri=…/oauth2/callback` and `scope=openid email profile` |
| Authentik accepts the client | `/authorize` answers `302` to the login flow — not `invalid_client` / unregistered `redirect_uri` |
| OIDC discovery at start-up | oauth2-proxy logs `Performing OIDC Discovery...` then configures, with no PKCE warning (`--code-challenge-method=S256`) |
| Group claim is released | the `profile` scope mapping returns `groups` as a list of group **names**, which is what `--allowed-group` matches |

**Not verified: a completed sign-in.** This environment holds no Authentik user
credential, so the handshake stops at the login screen. The `groups` claim and
the `--allowed-group` gate are therefore reasoned from the provider's own scope
mapping and Studio's working configuration, not observed. If the group name were
wrong the failure is **fail-closed** — every login is refused with a 403, nothing
is exposed. Confirm it once with a real account:

```bash
STUDIO_E2E_BASE_URL=https://gateway.olympus.innotel.us \
STUDIO_E2E_AUTHENTIK_URL=https://auth.cerulean.innotel.us \
STUDIO_E2E_USERNAME=<you> STUDIO_E2E_PASSWORD=<yours> \
  npx vitest run tests/integration   # from web/studio — see web/studio/README.md
```

Two further layers remain, deliberately. The dashboard's own password still
applies *behind* the proxy, so a session needs both Authentik and that password
(the stock `CHANGEME` was rotated off the documented default). And the proxy only
ever sees credentials over HTTPS because the edge terminates TLS.

## What still needs the operator

1. **`CERULEAN_ADMIN_PASSWORD` is still the template placeholder**, so the DNS
   record and certificate cannot be created from here. The Cerulean control plane
   answers (`https://cerulean.innotel.us`, local login enabled) and it issues TLS
   and manages NPM — fill the value in `.env` and the record
   (`gateway.olympus.innotel.us` → `192.168.1.10`) and its certificate become
   automatable.
2. **A proxy host on the NPM edge** from `gateway.olympus.innotel.us` to
   `http://192.168.1.10:20129`. The NPM API is not exposed by Cerulean — it lists
   hosts and exports certificates to NPM, but creating a proxy host is a UI step.
   Point it at **20129, not 20128**: forwarding to the gateway directly would
   bypass the proxy and restore the unauthenticated dashboard.
3. **Keep the gateway on loopback.** The edge reaches the proxy, and the proxy
   reaches the gateway. Publishing 20128 as well undoes the point of this page.
4. **Tighten the forwarded headers once the edge's address is known.** The proxy
   is published on the LAN, and `--reverse-proxy=true` means it trusts
   `X-Forwarded-Proto`/`Host` from anyone who can reach 20129 — a direct caller
   could claim `https`. That buys no access (every path still needs an Authentik
   session in the allowed group), but `--trusted-ip=<edge>` closes it, and the
   edge's address is not knowable from here.

## Related

* `compose.gateway-sso.yml` — the proxy, and why it needs host networking.
* `scripts/authentik-studio-app.py` — the registration tool, and `make gateway-oidc`.
* `docs/stack.md` — how the stack fits together.
* `scripts/omniroute-vault.sh` — where the dashboard password comes from.
* `scripts/omniroute-restore-providers.py` — the other repair for a gateway that
  lost its shape.
