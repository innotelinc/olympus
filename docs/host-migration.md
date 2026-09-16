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
| `studio.olympus.innotel.us`, `olympus.innotel.us` | ~~`.10:3001`~~ → **`.46:3050`** (done) | ✅ re-pointed, login verified |
| `*.studio.olympus.innotel.us` (3 sites × published + preview) | `.10:20130` | pending — one container + vhost per site |
| `gateway.olympus.innotel.us` | `.10:20129` | pending — the OmniRoute SSO proxy |
| `secure.innotel.us` | `.10:8088` | pending — not part of this stack |

Studio's own state is a **named docker volume** (`olympus_studio-data` → `/app/data`),
which is why it does not travel with a checkout: a fresh host comes up with an
empty Studio, and an empty Studio looks a lot like a broken one.

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

* **Published sites** — `make site-publish SLUG=<slug>` on the new host rebuilds
  the container, the vhost and the NPM host (see [site-publishing.md](site-publishing.md)).
  Their per-app databases live with the app containers, so they move with them.
* **The gateway** — `make gateway-oidc`, `gateway-sso-up`, `gateway-edge`, then
  `make gateway-edge-check`, which walks the public name link by link and names
  the first broken one (see [gateway-sso.md](gateway-sso.md)).
* **The edge's proxy hosts** — re-point, then confirm with
  `python3 scripts/verify-sso.py`, which fails if any of them still names `.10`.

## Retiring `.10`

Do not decommission it until `status` shows the volume restored here **and** the
three site names answer from this host. Until then `.10` is the only copy of the
published sites, and it is not backed up by this repo.

`192.168.1.10` was also reachable for **SSH only** (port 22) — port 2375, the
unauthenticated Docker API, was closed, which is the one thing that must stay
true while it is still standing.
