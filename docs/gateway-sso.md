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
                       │  └──▶ redis at 127.0.0.1:16379   (the session lives here)
                       │  http://127.0.0.1:20128
                       ▼
                    omniroute (dashboard, still loopback-only)
```

Sessions are **server-side**, in redis, and that is load-bearing rather than a
preference — see [Why the session is not a cookie](#why-the-session-is-not-a-cookie).

The proxy is the front door; the gateway is unchanged and still published on the
host's loopback only. OmniRoute's **own** OIDC stays disabled, for the reason in
the next section — that is not a preference, it is the one path that cannot work.

| | |
| --- | --- |
| Public name | `gateway.olympus.innotel.us` — CNAME `gateway.olympus` → `innotel.us` in zone `innotel.us` |
| Certificate | Let's Encrypt, issued by Cerulean (its row #16), exported to NPM as certificate #174 |
| Edge | NPM proxy host #198 → `http://192.168.1.10:20129`, TLS enforced, websockets on |
| Proxy | `olympus-gateway-sso` (`quay.io/oauth2-proxy/oauth2-proxy:v7.7.1-alpine`), host networking, listens `0.0.0.0:20129` |
| Session store | `olympus-gateway-sso-sessions` (`redis:7-alpine`), loopback only on `127.0.0.1:16379`, password from `GATEWAY_SSO_REDIS_PASSWORD` |
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
| `/v1/*` | anyone who can reach `192.168.1.10:20129` — **no key is checked** (see below) | inference clients send `Authorization: Bearer …`, not a session cookie, so an interactive OIDC login here would break every one of them rather than add a check. The door is the LAN address, and **the public name refuses this path at the edge** |
| `/healthz` | anyone | liveness, 200 with no body and no secrets |
| everything else, including `/dashboard` and `/api/providers` | an Authentik session in `cerulean-platform` | this is the surface that reads and writes provider credentials |

> **`/v1` is not key-authenticated on this gateway — measured.** No key, a bogus key and
> the real `OMNIROUTE_API_KEY` all answer `200`, and an unauthenticated
> `POST /v1/chat/completions` returns a completion. `make gateway-auth-mode` sets
> `requireLogin=false` (its own docstring: the loopback binding is then the entire
> control) and this OmniRoute build validates nothing on `/v1`. That was harmless while
> the gateway was loopback-only; combined with the exemption above it meant the public
> name published the provider credentials behind it. The public name now refuses `/v1`
> at the edge — `make gateway-edge --deny-path /v1`, asserted by
> `make gateway-edge-check` — and the LAN address above is how a client on another
> machine reaches inference. Treat the key as a label, not a gate.

`--skip-auth-route` is what draws that line. `/api/auth/login` is deliberately
**not** on it: that is the dashboard's own password login, and exempting it would
hand anyone holding that password a dashboard session that never touched
Authentik — the group check would become decoration. Signing in is SSO first,
then the dashboard password.

So "reachable on the LAN" and "the dashboard is not exposed" are both true, and
neither is achieved by changing the gateway's binding:

```bash
# from any machine on the LAN — the address, not the name
curl -H "Authorization: Bearer $OMNIROUTE_API_KEY" http://192.168.1.10:20129/v1/models
OMNIROUTE_BASE_URL=http://192.168.1.10:20129/v1
```

**Not the published name.** `https://gateway.olympus.innotel.us/v1/*` answers `403` by
edge rule: that name exists to reach the dashboard, and `/v1` there handed the internet
an unauthenticated inference API. A client on another machine points
`OMNIROUTE_BASE_URL` at the LAN address; nothing else about its setup changes. It can
send the key — the key is not what is being checked.

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
| LAN inference | `http://192.168.1.10:20129/v1/models` → `200`, with or without the API key — the key is not validated (below) |
| Public inference | `https://gateway.olympus.innotel.us/v1/models` and `/v1/chat/completions` → `403`, refused at the edge; `/v1` (bare) → `403` too |
| The edge rule was actually written | NPM host #198 `advanced_config` reads back the two `location` lines — read back, because Cerulean's NPM passthrough accepts `advanced_config` and drops it (200, `modified_on` updated, field still empty) |
| The key is not a gate | `POST /v1/chat/completions` with no `Authorization` at all → `200` and a completion, on the LAN path |
| LAN dashboard | `/dashboard`, `/login`, `/api/providers`, `/` all → `302` to Authentik from `192.168.1.10:20129` |
| Gateway stays private | `http://192.168.1.10:20128/healthz` → no connection |
| One OmniRoute only | one container, one listener on `20128`; the `olympus` container no longer publishes that port and no longer starts a bundled gateway |
| `make gateway-edge` re-run | reports all three steps already done; writes nothing |
| Scanner traffic | internet scanners that found the new name (it is minutes old) get the same `302` to Authentik, never the dashboard |
| A completed sign-in | driven end to end through the Authentik flow executor: identification → password → authorize → `/oauth2/callback` → `302 /`, then `/dashboard` answered **200** — OmniRoute's own `/login` is never reached |
| One gate only | `make gateway-auth-mode --verify` → exit 0; the gateway's own login is off |
| The gateway does not gate itself | `GET /api/providers` on `127.0.0.1:20128` answers `200` with no session, which is what `requireLogin=false` means |
| Nothing but the proxy can reach it | `192.168.1.10:20128` and `192.168.1.10:16379` both refuse, so reachability is the whole control and the proxy holds it |

The sign-in is no longer the thing that is unverified — that paragraph used to say
it was, and then the broken version of exactly that step (see the next section)
went unnoticed for want of it. Driving it is what found the 502.

The proxy only ever sees credentials over HTTPS because the edge terminates TLS.

## One gate, and it is Authentik

There used to be two logins for one surface: the proxy, then OmniRoute's own
dashboard password. The second guarded nothing — the proxy had already established
who you were, and the management API it protected is reachable only through the
proxy — and it hid the fact that the *first* one is the only one that can work,
because OmniRoute's own OIDC cannot be enabled (the `iss` mismatch at the top of
this file). Two credential systems, one of which cannot authenticate anybody.

`make gateway-auth-mode` removes the gateway's own login:

```
make gateway-auth-mode --dry-run     # what it would do
make gateway-auth-mode               # do it
make gateway-auth-mode --verify      # exit non-zero unless Authentik is the only gate
```

**What it actually does, measured on a throwaway instance rather than assumed:**
`requireLogin=false` makes the gateway stop gating its own management API.

| | before | after |
| --- | --- | --- |
| `GET /api/settings` with no session | `401 Authentication required` | `200` |
| `GET /api/providers` with no session | `401 Authentication required` | `200` |
| `GET /dashboard` with no session | `200` (the shell was always public) | `200` |

The dashboard is then served straight through, and the sign-in that mattered is the
Authentik one. **This makes the reachability of port 20128 the entire control**, and
that is why the script checks the binding before it changes anything and refuses if
it is not loopback — see below. It is not a preference; without that binding this
change would be a hole.

The stored dashboard password is **kept**. It grants nothing once `requireLogin` is
false, and it is the way back in if a future release resets the flag: `requireLogin =
true` with no password is a lockout needing a volume edit. Removing it would have
been the tidier-looking change and the worse one.

The order matters and is enforced by the API, not by us: `requireLogin` is a
security-impacting setting, so OmniRoute answers `400 PASSWORD_REQUIRED` unless the
current password travels with the request — a hijacked session cannot open the
dashboard on its own. The script resolves that password from the `vault://`
reference in `.env` and sends it.

## Why the session is not a cookie

**Symptom:** `https://gateway.olympus.innotel.us` loads the Authentik login, you
sign in, and the browser gets **`502 Bad Gateway`** from the edge. Every probe at
the front door says the name is fine, because it is: the name, the certificate,
the edge and the proxy are all healthy. The request that fails is the one that
would have finished the login.

**Cause:** oauth2-proxy was storing the session in a cookie. A cookie session
carries the email, the ID token and **every group the identity claims** — and
`dhunter` is in thirty Authentik groups, so the serialized session exceeds the 4KB
a cookie can hold. The proxy says so on every login and splits the session across
several cookies:

```
WARNING: Multiple cookies are required for this session as it exceeds the 4kb
cookie limit. Please use server side session storage (eg. Redis) instead.
```

Those extra `Set-Cookie` headers make the callback's **response header** larger
than the edge's `proxy_buffer_size`, and nginx refuses rather than truncate. In
NPM's log for proxy host #198:

```
upstream sent too big header while reading response header from upstream
  request: "GET /oauth2/callback?code=7e7eaf2ac62e496c894e3669eee0b5e6&state=…"
  upstream: "http://192.168.1.10:20129/oauth2/callback?code=…"
```

That is a `502`, and it lands on the callback, so it reads as "the gateway is
down" — while the gateway was up and the URL worked perfectly.

**Fix:** `--session-store-type=redis`, with `olympus-gateway-sso-sessions` holding
the sessions and the browser holding one opaque id. The ceiling is gone rather
than raised: it no longer matters how many groups an identity accumulates. Measured
on the same callback through the same edge, after the change: 2 cookies, 367 bytes
of `Set-Cookie` in total, 687 bytes of response headers.

Raising the edge's buffer was the other candidate and was rejected: it treats the
symptom, and the cookie would still have to be *sent back* on every request, where
nginx's `large_client_header_buffers` is the next limit to hit. Server-side sessions
remove the class of failure.

The store is loopback-only, holds nothing but sessions, and has no volume —
losing it costs a re-login and nothing else, which is why it is the one container
here that is safe to restart unattended.

## "It's not resolving" — check the chain, don't guess

The name is five things in series, and every one of them fails with the same
sentence in a browser:

```
DNS        gateway.olympus.innotel.us  CNAME  innotel.us  → A  73.68.203.71
edge       NPM (192.168.1.71) :443     →  http://192.168.1.10:20129
proxy      oauth2-proxy                →  Authentik, for everything but /ping
session    oauth2-proxy                →  redis at 127.0.0.1:16379
gateway    omniroute, loopback only     →  127.0.0.1:20128
```

A resolver that does not answer is a DNS error. A dead edge is a connection
timeout. A lapsed certificate is a privacy warning. A stopped proxy is a 502. All
four are reported as "it's not resolving", and three of them are not the name.

`make gateway-edge-check` walks the chain in order, reports every link, and names
the first broken one:

```
$ make gateway-edge-check
  gateway.olympus.innotel.us
    1. dns
       system    127.0.0.53       NOERROR via innotel.us 73.68.203.71
       cerulean  192.168.1.46     NOERROR via innotel.us 73.68.203.71
    ok   tls    YR2 · expires Dec 12 12:24:25 2026 GMT (89d)
    ok   edge   HTTP 302 · openresty · redirects to the identity provider, as the SSO proxy should
    ok   proxy  HTTP 200 · OK
    ok   session 127.0.0.1:16379 · sessions in redis · up and answering PING

ok: gateway.olympus.innotel.us is reachable and gated as expected.
```

The `session` link is the odd one out and the reason it was added: it is the only
part of the chain that **cannot be reached with `curl`**, because the failure only
exists after you authenticate. So it checks the two things that make a login work —
the store answers `PING` with the configured password, and the running proxy is
pointed at it rather than at a cookie — and it says which of those it could not
verify (`not verified — no docker on this host`) instead of implying the rest. It
is left out entirely under `--no-sso`, since a published site has no login to fail.

Exit `0` is reachable, `1` is broken with the link named, `2` is "the check could
not run". `--host` checks another name (a published site, say, with `--no-sso`),
`--json` is for anything that wants to consume it, and `--resolver` overrides the
platform resolver it asks alongside the system one.

`scripts/gateway-edge-alert.sh` runs it daily under
`olympus-gateway-edge-check.timer` (`sudo scripts/install-token-check-timer.sh
TARGET=gateway-edge`) and Telegrams the report when a link is broken. It is the one
check in that set that looks outward rather than at this host, which is exactly why
it exists.

### The finding that makes this worth having

The record is a CNAME to the zone apex, and both of the zone's own nameservers —
`ns1.innotel.us` and `ns2.innotel.us` — resolve to **the same address**, on the same
host as the edge, Authentik and Cerulean. So when that host is down, every name
under `innotel.us` stops resolving at once: internally and publicly, `gateway`,
`studio`, `auth` and the rest. It presents as one name failing to resolve and it is
in fact a platform outage, so the report says *which resolver failed and which
answered* rather than just failing.

Two nameservers on one host is not redundancy, and that is a network change rather
than something this repo can make. What this repo can do is make the next
occurrence say so in one line instead of costing an afternoon.

## The one thing left worth doing by hand

**Close port 20129 to everything but the edge.** The proxy listens on `0.0.0.0`
so the edge (a different host) can reach it, which also means anything else on
the LAN can. That is not an authentication problem — every path still needs an
Authentik session in the allowed group — but it is unnecessary surface, and it
matters more than it used to: the proxy is now the *only* gate, so an open 20129
is an open door to a gateway that no longer authenticates anything itself.

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

* `compose.gateway-sso.yml` — the proxy, the session store, and why the proxy needs host networking.
* `scripts/cerulean-edge.py` — DNS + certificate + edge host, and `make gateway-edge`.
* `scripts/gateway-edge-check.py` — the five-link check above, and `make gateway-edge-check`.
* `scripts/authentik-studio-app.py` — the client registration, and `make gateway-oidc`.
* `docs/stack.md` — how the stack fits together.
* `scripts/omniroute-vault.sh` — where the dashboard password comes from.
