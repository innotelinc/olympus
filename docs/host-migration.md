# Moving Olympus onto one host

**Target: `192.168.1.46` (this checkout's host). Legacy: `192.168.1.10`.**

Olympus ran on two hosts at once, and for a while nobody could tell. The edge
published `studio.olympus.innotel.us` from `192.168.1.10`, while the checkout —
with its `.env`, its `verify-sso.py` and its `OIDC_CLIENT_SECRET` — lives on
`192.168.1.46`, which also runs a Studio. Both were healthy. They simply were not
the same deployment, and the public name was serving the older one.

## What that looked like from the outside

Every sign-in died with:

```
Sign-in failed: Token endpoint returned 400 —
{"error": "invalid_client", "error_description": "Client authentication failed
 (e.g., unknown client, no client authentication included, or unsupported
 authentication method)"}
```

Three things conspire to make that message useless:

* Authentik stores a provider's secret **write-only**, so the configured value
  cannot be read back or compared by hand;
* Authentik answers `invalid_client` for **two different faults** — a wrong
  secret *and* an unregistered `redirect_uri` at the token endpoint — so the
  body does not say which;
* the message names the client, not the host, so "which Studio is answering?"
  is not a question the error raises.

The diagnosis that works is to separate the two faults, and to check the edge:

```bash
# which host is the public name actually served from?
python3 scripts/verify-sso.py --verbose   # prints the NPM forward target

# do the credentials in this checkout authenticate at all?
OIDC_CLIENT_SECRET=wrong python3 scripts/verify-sso.py   # must FAIL
```

`verify-sso.py` asserts both. Its credential probe sends a client authentication
that *can* succeed with a code that *cannot*, so `invalid_grant` means Authentik
accepted the client and `invalid_client` means it did not — a verdict, not a
guess. It then compares the proxy host's forward target against this host, because
a correct checkout served from the wrong box fails exactly like a wrong secret.

Confirmed here: `192.168.1.10`'s Studio held a credential Authentik rejected, and
`.46`'s `.env` was correct all along.

## What is where

| Name | Served from | Move |
| --- | --- | --- |
| `studio.olympus.innotel.us`, `olympus.innotel.us` | ~~`.10:3001`~~ → **`172.17.0.1:3050`** (done) | ✅ re-pointed, login verified |
| `*.studio.olympus.innotel.us` (3 apps × name + preview) | ~~`.10:20130`~~ → **`172.17.0.1:20130`** | ✅ apps + data moved, API verified through every name |
| `gateway.olympus.innotel.us` | ~~`.10:20129`~~ → **`172.17.0.1:20129`** | ✅ re-pointed, SSO gate verified |
| `secure.innotel.us` | `.10:8088` — dead before the move | ⛔ retired; see below |
| Studio's data volume + packaged builds | ~~`olympus_studio-data` on `.10`~~ | ✅ imported here, SHA-256-verified file for file |

The Studio upstream is the **docker0 gateway**, not the LAN address, because Studio
is no longer published on every interface: it binds `127.0.0.1` and `172.17.0.1`
only (`docker-compose.yml`, `STUDIO_EDGE_HOST`). Anyone on the LAN could otherwise
reach the app directly and skip the edge — and with it the sign-in the edge does.
The edge is itself a container on its own bridge, so it cannot reach a
loopback-only port and dials the gateway instead, the same way the DNS console is
reached. If the store address ever needs to move again, it is `STUDIO_EDGE_HOST`
in `.env` **and** the `forward_host` on both NPM proxy hosts. The same pair now
holds for the sites (`SITE_EDGE_FORWARD_HOST`) and the gateway, so **every**
migrated name dials `172.17.0.1` and none of them names `.10` any more — a
property worth re-checking first, because a stale `forward_host` looks exactly
like a broken backend.

Studio's own state is a **named docker volume** (`olympus_studio-data` → `/app/data`),
which is why it does not travel with a checkout: a fresh host comes up with an
empty Studio, and an empty Studio looks a lot like a broken one.

## Moving the published sites — and what they turned out to be

**Correction to an earlier version of this document.** It used to say the three
apps were static sites and that two of the six names (the `-preview` pair) were
"404 on `.10` too — their directories were never staged". **Both statements were
wrong**, and the second one was only discoverable with shell access to `.10`:

* every one of the six names is a **Studio application** — a container with an API
  and a SQLite database — fronted by `olympus-sites` through a generated per-slug
  vhost (`scripts/app-runtime.py`), not a staged static directory. `weight-tracker`
  answered `/api/health` with `{"ok":true,"tables":["weigh_ins"]}`; the other two
  answered every path with their SPA, which is what a single-page app does and not
  evidence of a missing API.
* the `-preview` names are **aliases of the same running app** (nginx
  `server_name foot-fetish-site-preview…` → the same `127.0.0.1:21401`), not
  separate previews. From the edge they answered 404 on `.10` because the edge's
  forward went to a *different* sites server than the one that had the vhosts —
  and from here, before the apps were moved, because this host had no vhosts at
  all. "404" never meant "no content".

The first attempt at this move used `scripts/mirror-published-site.py`, which
fetches a name and re-serves it as static files. That preserved bytes and lost
the product: the pages loaded, `/api/*` 404'd, and `weight-tracker`'s three real
weigh-ins were unreachable through every name. The tool is kept as a recovery of
last resort — its docstring now says so — but the migration is done the way the
repo already documents it:

```bash
# the artifacts, from the old host (small: 87 MB of builds, 3 KB of SQLite)
tar czf - -C <old-checkout> builds/<slug>            | tar xzf - -C <this-checkout>   # packaged sources
tar czf - -C /var/lib/olympus/apps runtime data nginx | tar xzf - -C /var/lib/olympus/apps  # state + data
docker save olympus-app-<slug>:latest | gzip          > apps.tgz                      # or rebuild from builds/

# here — the same three commands the repo documents, per slug
python3 scripts/app-runtime.py --up <slug> --preview
```

`--up` reuses the restored runtime record, so each app comes back on its original
port (21400–21402), mounts its original data directory, writes both vhosts
(name + `-preview` alias), and reloads `olympus-sites` — after which the edge
needs no change at all, because it has always forwarded these names to the sites
port.

| Name | What it is | Port | Verified |
| --- | --- | --- | --- |
| `weight-tracker` + `-preview` | app, `weigh_ins` table | 21400 | 3 weigh-ins identical over the API |
| `foot-fetish-site` + `-preview` | app, informational (no tables) | 21401 | serves, API behaves as on `.10` |
| `resume-generator` + `-preview` | app (static build, no API) | 21402 | serves, form state client-side |

The static copies under `$OLYMPUS_SITES_ROOT/<slug>/` made by the mirror tool are
still on disk. They are now shadowed — an exact `server_name` vhost beats the
wildcard static template — and they are **stale the moment an app changes**. They
stay only as a recovery fallback; do not treat them as the source of anything.

## Retiring `secure.innotel.us`

The name pointed at `.10:8088`, which was **already dead** — the edge answered
`502`, so the premise that it was "still served from .10" did not hold. The only
live thing on that port here is Asterisk's own HTTP server (`Server: Asterisk/22.11.0`
from `zeus-freepbx`), which serves `/ari` (401) and `/ws` (websocket upgrade) and
nothing browsable; `/`, `/admin` and `/ucp` are all 404.

What made the decision is that **nothing in the estate owns the name**. It appears
in exactly one other place — an old line in this table — while the voice stack's
canonical names are all `*.zeus.innotel.us` and `8088` is documented there as
"Asterisk HTTP (LAN + edge proxy)". Re-pointing it would have restored a public
route to a telephony management API that no repository, compose file or `.env`
asks for. So it was retired instead:

```bash
# NPM host #130, disabled — not deleted
POST /api/nginx/proxy-hosts/130/disable
```

The host, its certificate (#34) and its forward target stay in NPM, so restoring it
is one call; but NPM no longer generates the server block, so the name is now
**refused** rather than proxied. If someone does need it, `pbx.zeus.innotel.us` is
the name the voice stack actually documents.

## Moving Studio's data

`scripts/migrate-studio-data.py` bundles the volume; it never writes to it during
export, and the import refuses to overwrite a non-empty volume without `--force`.

```bash
# on 192.168.1.10 (needs shell access there — the one step this repo cannot do for you)
cd <olympus checkout on .10>
python3 scripts/migrate-studio-data.py export --out /tmp/olympus-handover

# bring it across
scp -r root@192.168.1.10:/tmp/olympus-handover /tmp/olympus-handover

# on 192.168.1.46
cd 5-dev/olympus
python3 scripts/migrate-studio-data.py status          # should be empty here
python3 scripts/migrate-studio-data.py import --in /tmp/olympus-handover
docker compose up -d studio
python3 scripts/verify-sso.py
```

`status` is the honest check on both ends: it reports the entry count and size, so
"nothing came across" is visible before anyone starts debugging sign-in again.

Then, each with its own pipeline rather than this tool:

* **Published sites** — the sanctioned path is `make app-publish SLUG=<slug>` on the
  new host, which packages the app, runs it and wires the name (see
  [site-publishing.md](site-publishing.md)). It needs Studio's projects, which now
  live in the volume imported here — the artifacts came across with
  `migrate-studio-data.py` and the apps were brought up with
  `app-runtime.py --up <slug> --preview`; see
  [Moving the published sites](#moving-the-published-sites). Their per-app
  databases live with the app containers, so they move with them.
* **The gateway** — `make gateway-oidc`, `gateway-sso-up`, `gateway-edge`, then
  `make gateway-edge-check`, which walks the public name link by link and names
  the first broken one (see [gateway-sso.md](gateway-sso.md)).
* **The edge's proxy hosts** — re-point, then confirm with
  `python3 scripts/verify-sso.py`, which fails if any of them still names `.10`.

## Retiring `.10`

Everything this estate served from `.10` is now served from here and has been
verified with `.10`'s own services **stopped**: Studio (sign-in verified end to end,
projects list served), the three apps (all six names, APIs and the weigh-ins data),
the gateway chain, and the sites server. The Studio volume was checked file for
file by SHA-256. What `.10` still holds is now only history: its containers are
stopped, its images and volume remain as a last-resort rollback, and nothing in
the estate references it — no proxy host names it, and no compose file or `.env`
points at it.

The password given for SSH access to `.10` was shared in a chat, so rotate it (or
disable password authentication for root there) before treating the host as
closed. It was reachable for **SSH and ports 80/3001/20130** while being retired;
port 2375, the unauthenticated Docker API, stayed closed throughout, which is the
one thing that must remain true if the host is kept standing.
