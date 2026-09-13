# Putting the gateway dashboard behind Cerulean Authentik

The OmniRoute dashboard manages provider credentials and can read them, so it
should not be reachable on a shared password. It is fronted by an identity-aware
proxy that authenticates against Cerulean Authentik and admits only members of
`cerulean-platform`, and it is published at
**https://gateway.olympus.innotel.us** with a Let's Encrypt certificate:

```
browser ──https──▶ NPM edge (192.168.1.71)
                       │  gateway.olympus.innotel.us  (CNAME → innotel.us → 73.68.203.71)
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
| Public name | `gateway.olympus.innotel.us` — CNAME `gateway.olympus` → `innotel.us` in zone `innotel.us` |
| Certificate | Let's Encrypt, issued by Cerulean (its row #16), exported to NPM as certificate #174 |
| Edge | NPM proxy host #198 → `http://192.168.1.10:20129`, TLS enforced, websockets on |
| Proxy | `olympus-gateway-sso` (`quay.io/oauth2-proxy/oauth2-proxy:v7.7.1-alpine`), host networking, listens `0.0.0.0:20129` |
| Upstream | `http://127.0.0.1:20128` (the gateway's loopback binding) |
| LAN path | `/v1/*` and `/healthz` pass through; everything else — dashboard included — requires Authentik |
| Authentik application | `OmniRoute Gateway` (slug `omniroute`, provider pk 30) |
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
make gateway-sso-up                      # run the proxy
make gateway-edge ARGS="--dry-run"       # what the DNS/cert/edge step would do
make gateway-edge                        # publish the name
make gateway-sso-check
```

`--rotate-secret` exists because Authentik stores client secrets write-only: a
provider that already exists has no readable value, so rotation is the only way
to obtain one. The previous secret stops working the moment it runs. An ordinary
re-run — which repairs `grant_types` and missing redirect URIs — never touches
the secret, and `scripts/tests/test_authentik_app.py` pins both behaviours.

### `make gateway-edge`

`scripts/cerulean-edge.py` runs the three provisioning steps Cerulean's own API
already supported, in the order that works: **DNS record → certificate → export
to NPM → proxy host.** The order is load-bearing — certificate validation needs
the name to resolve, and a host with `ssl_forced` needs a certificate to point at.

It is idempotent and it is deliberately unwilling:

* a record that already answers somewhere else is **not** repointed (that is how
  one service takes another's traffic), and neither is a proxy host;
* a certificate is reused only if it is `issued`, actually carries material, and
  lasts longer than `--renew-days` (default 30) — an `issued` row with no PEM is
  what a failed export leaves behind;
* a value equal to its `.env.example` placeholder is **refused**, not used. This
  one is measured, not hypothetical: this platform's process environment exports
  `CERULEAN_ADMIN_PASSWORD=change-me-cerulean-admin`, which under "the environment
  wins" shadowed a working `.env` and turned the whole thing into an opaque
  `HTTP 401`. The script says which source it skipped and why.

`GATEWAY_SSO_EDGE_FORWARD_HOST` must be **this host's LAN address** — the address
running the proxy. Pointing the edge at the gateway (20128) instead would bypass
the proxy and restore the unauthenticated dashboard.

## Using the gateway from another machine on the LAN

The gateway itself listens on `127.0.0.1:20128` and stays that way: it is the
process that holds every provider credential, and widening its binding puts the
dashboard on the network. The **proxy** is the LAN door — `0.0.0.0:20129` — and it
sorts callers by what they are:

| Path | Who gets in | Why |
| --- | --- | --- |
| `/v1/*` | anyone with a valid `Authorization: Bearer $OMNIROUTE_API_KEY` | inference is already key-authenticated. An interactive OIDC login in front of it would not add a check, it would break every client: Codex and the CLIs send a key, not a session cookie |
| `/healthz` | anyone | liveness, 200 with no body and no secrets |
| everything else, including `/dashboard` and `/api/providers` | an Authentik session in `cerulean-platform` | this is the surface that reads and writes provider credentials |

`--skip-auth-route` is what draws that line. `/api/auth/login` is deliberately
**not** on it: that is the dashboard's own password login, and exempting it would
hand anyone holding that password a dashboard session that never touched
Authentik — the group check would become decoration. Signing in is SSO first,
then the dashboard password.

So "reachable on the LAN" and "the dashboard is not exposed" are both true, and
neither is achieved by changing the gateway's binding:

```bash
# from any machine on the LAN, inference with the key from .env
curl -H "Authorization: Bearer $OMNIROUTE_API_KEY" http://192.168.1.10:20129/v1/models

# or by the published name (DNS + TLS via Cerulean)
OMNIROUTE_BASE_URL=https://gateway.olympus.innotel.us/v1
```

A client on another machine points `OMNIROUTE_BASE_URL` at either of those and
uses the same key; nothing else about its setup changes.

## What was verified, and what was not

Verified against the live deployment:

| Check | Result |
| --- | --- |
| Public DNS | `gateway.olympus.innotel.us` → `73.68.203.71`, same as the other `*.olympus` names |
| Public TLS | `CN=gateway.olympus.innotel.us`, issuer Let's Encrypt `YR2`, valid 13 Sep → 12 Dec 2026 |
| HTTP → HTTPS | `301 Moved Permanently` to `https://gateway.olympus.innotel.us/` |
| Public `/` | `302` to `https://auth.cerulean.innotel.us/application/o/authorize/` with `client_id=omniroute`, PKCE `S256`, and the registered `redirect_uri` |
| Authentik accepts the client | `/authorize` answers `302` to the login flow — not `invalid_client` / unregistered `redirect_uri` |
| `make gateway-sso-check` | ok — live on `127.0.0.1:20129`, `/` redirects to Authentik as client `omniroute` |
| Proxy on the LAN | `/ping` → `200` from `192.168.1.10:20129` as well as loopback |
| LAN inference | `http://192.168.1.10:20129/v1/models` → `200` with the API key, `401` without it |
| LAN inference by name | `https://gateway.olympus.innotel.us/v1/models` → `200` with the key, `401` without |
| LAN dashboard | `/dashboard`, `/login`, `/api/providers`, `/` all → `302` to Authentik from `192.168.1.10:20129` |
| Gateway stays private | `http://192.168.1.10:20128/healthz` → no connection |
| One OmniRoute only | one container, one listener on `20128`; the `olympus` container no longer publishes that port and no longer starts a bundled gateway |
| `make gateway-edge` re-run | reports all three steps already done; writes nothing |
| Scanner traffic | internet scanners that found the new name (it is minutes old) get the same `302` to Authentik, never the dashboard |

**Not verified: a completed sign-in.** This environment holds no Authentik user
credential, so the handshake stops at the login screen. The `groups` claim and
the `--allowed-group` gate are therefore reasoned from the provider's own scope
mapping — `profile` returns `groups` as a list of group **names**, which is what
`--allowed-group` matches — and from Studio's working configuration, not observed.
If the group name were wrong the failure is **fail-closed**: every login is
refused with a 403, nothing is exposed.

Two further layers remain, deliberately. The dashboard's own password still
applies *behind* the proxy, so a session needs both Authentik and that password
(the stock `CHANGEME` was rotated off the documented default; the value is in
Cerulean Vault — `scripts/omniroute-vault.sh`). And the proxy only ever sees
credentials over HTTPS because the edge terminates TLS.

## The one thing left worth doing by hand

**Close port 20129 to everything but the edge.** The proxy listens on `0.0.0.0`
so the edge (a different host) can reach it, which also means anything else on
the LAN can. That is not an authentication problem — every path still needs an
Authentik session in the allowed group — but it is unnecessary surface.

`--trusted-ip` was tried and **removed**: oauth2-proxy warns on every request that
mixing it with `--reverse-proxy` is unsafe, and it is right — with reverse-proxy
on the client IP is read from `X-Forwarded-For`, so a caller who can reach the
port spoofs the trusted address and is believed. The control has to be at the
network layer:

```bash
# on the host running the proxy, allowing only the edge
iptables -I INPUT -p tcp --dport 20129 ! -s 192.168.1.71 -j DROP
```

That is deliberately not applied here: this host has no firewall in force
(`iptables -S` shows default-ACCEPT policies), and introducing one unasked is a
bigger change than the exposure it closes.

## Related

* `compose.gateway-sso.yml` — the proxy, and why it needs host networking.
* `scripts/cerulean-edge.py` — DNS + certificate + edge host, and `make gateway-edge`.
* `scripts/authentik-studio-app.py` — the client registration, and `make gateway-oidc`.
* `docs/stack.md` — how the stack fits together.
* `scripts/omniroute-vault.sh` — where the dashboard password comes from.
