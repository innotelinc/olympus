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
| `*.studio.olympus.innotel.us` (3 sites × published + preview) | ~~`.10:20130`~~ → **`172.17.0.1:20130`** | ✅ re-pointed, every fetched byte verified |
| `gateway.olympus.innotel.us` | ~~`.10:20129`~~ → **`172.17.0.1:20129`** | ✅ re-pointed, SSO gate verified |
| `secure.innotel.us` | `.10:8088` — dead before the move | ⛔ retired; see below |

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

## Moving the published sites

The staged directories are the awkward part of this move. A published site is not
a build artefact in git: `package-website.py` renders it into
`$OLYMPUS_SITES_ROOT/<slug>/` and the `sites` container serves that directory — and
the same host holds Studio, so `make site-package` cannot be re-run here without
Studio's projects. The published names were therefore the only copy reachable
without shell access to `.10`, and they are HTTP.

`scripts/mirror-published-site.py` fetches a name and writes what it serves:

```bash
# what it would copy (default is a dry run)
python3 scripts/mirror-published-site.py \
    --host foot-fetish-site.studio.olympus.innotel.us \
    --dest /var/lib/olympus/sites/foot-fetish-site

# copy it, recording a per-path manifest beside the directory
python3 scripts/mirror-published-site.py \
    --host foot-fetish-site.studio.olympus.innotel.us \
    --dest /var/lib/olympus/sites/foot-fetish-site --apply
```

It is a **recovery** tool, not the publish path, and it says so: it cannot see a
file the site never links to, so it records the manifest and lists anything
unreachable rather than dropping it silently. `--verify-base https://<name>` then
fetches each recorded path back and compares SHA-256 — which is the check that
matters, because "the directory exists" and "the name serves the right bytes" are
different claims.

Recovered and verified here, **12 files across 4 directories, all byte-identical**:

| Name | Files |
| --- | --- |
| `foot-fetish-site`, `resume-generator`, `weight-tracker` | index + 2 assets each |
| `weight-tracker-preview` | same 3 files as `weight-tracker` |
| `foot-fetish-site-preview`, `resume-generator-preview` | **none** — 404 on `.10` too |

The two preview names were already 404 before the move, because their directories
were never staged on `.10`. They still are, which is the correct outcome: the move
preserved what was actually being served, not what the naming scheme implies should
be. Republishing them is `make site-publish` against live Studio sources, and it is
a separate decision from "do not lose what is published".

One honest difference survives: nginx serves `.js` as `application/javascript`
where the origin sent `text/javascript`. The bytes and their hashes match; only the
header differs, and both are valid JavaScript MIME types.

Two operational notes for whoever finishes the move. The server is the `sites`
profile of `docker-compose.yml`, so it starts with `make sites-up` and a bare
`docker compose up -d` will **not** recreate it — but its restart policy is
`unless-stopped`, so it does come back on its own after a reboot. And the staged
directories are a read-only mount of `$OLYMPUS_SITES_ROOT` that lives outside git
(only the vhosts and the tooling are in the repo), which is exactly why each one is
paired with a manifest of the bytes it was recovered from.

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

* **Published sites** — the sanctioned path is `make site-publish SLUG=<slug>` on
  the new host, which rebuilds the container, the vhost and the NPM host (see
  [site-publishing.md](site-publishing.md)). It needs Studio's projects, which are
  still on `.10`, so the staged directories were recovered over HTTP instead — see
  [Moving the published sites](#moving-the-published-sites). Their per-app
  databases live with the app containers, so they move with them.
* **The gateway** — `make gateway-oidc`, `gateway-sso-up`, `gateway-edge`, then
  `make gateway-edge-check`, which walks the public name link by link and names
  the first broken one (see [gateway-sso.md](gateway-sso.md)).
* **The edge's proxy hosts** — re-point, then confirm with
  `python3 scripts/verify-sso.py`, which fails if any of them still names `.10`.

## Retiring `.10`

Do not decommission it until `status` shows the volume restored here. The site
names now answer from this host and every fetched byte has been verified against
the origin, so what `.10` still holds uniquely is **the staged sites' sources in
Studio** — the directories were recovered, but regenerating them still needs those
projects. Nothing else on `.10` is load-bearing any more: no proxy host names it,
and nothing else in the estate references it.

`192.168.1.10` was also reachable for **SSH only** (port 22) — port 2375, the
unauthenticated Docker API, was closed, which is the one thing that must stay
true while it is still standing.
