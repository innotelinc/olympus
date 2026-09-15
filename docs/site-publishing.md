# Publishing a Studio build

Studio builds two kinds of thing, they are different products, and neither is
finished when it is generated.

| | **App** | **Website** |
|---|---|---|
| What it is | A full-stack application: React client + Node HTTP API + SQLite | A Vite + React 19 + TypeScript project |
| Has state | Yes — a database, so what someone enters today is there tomorrow | No — static files, with nowhere to write |
| The model writes | `src/App.tsx`, `server/schema.sql` | `src/App.tsx` (+ components, styles) |
| Preview | None until it is running; JSX needs a build and the API needs a server | None until it is built; JSX needs a compiler |
| Finished when | `dist/client` exists **and** the container answers `/api/health` | `dist/` exists |
| Delivered as | A container behind `<slug>.<suffix>`, or a zip of client + server + Dockerfile | `dist/` on a name, or a zip of source + `dist/` |

Nothing about this is a preference. A React site is a build artifact, and an
application is a process with a database. The only honest place to say so is here.

## The deliveries

Studio offers one thing per delivery, and each has exactly one job. They used to
overlap — one button rebuilt *and* published — which made the words useless for
deciding which to press.

| | What it does | What it does not do |
|---|---|---|
| **Build It** | The model writes, or adds on to, the app in Studio | It does not touch the factory |
| **Factory Build** | Runs `make app` (Archon + the model) from a spec, then packages whatever came out | It does not publish a name |
| **Preview It** | Packages **the files on screen**, runs them, and frames the running project on `<slug>-preview.<suffix>` | It does not rebuild, and it does not register the project's own name — see below |
| **Publish It** | Packages **the files on screen**, runs an app, and puts the name in front of it | It does not rebuild. Publishing what a rebuild would produce, rather than what you are looking at, is a different build with the same name |
| **Export It** | Writes a `build-requests/` spec for CI or a hand-off, with that kind's next steps | It does not build |
| **Download It** | A zip of the source — and of the built output once packaged | It does not publish |

### Preview It — the project running, under a name of its own

```bash
python3 scripts/package-project.py <slug>
python3 scripts/app-runtime.py --up <slug> --build --preview
python3 scripts/studio-sites.py --preview <slug>
```

The preview has to be reachable from the browser showing it: the pane is an https
document, and an iframe of a plain-http address is blocked as mixed content. So a
preview needs a name — just not the project's own. `--preview` writes a second
vhost for the same container (`<slug>-preview.<suffix>`), and
`studio-sites.py --preview` puts that name on the edge, covered by the same
wildcard certificate so it is seconds rather than a certificate order. The project's
own name is never added, which is the whole difference from **Publish It**: a
preview of something nobody has published produces a name that is a preview and
nothing else.

Two consequences worth knowing:

* The container is the project's own, so a project that **is** published serves the
  previewed files on its real name until the next publish. One project, one
  container — the preview is not a second copy of it.
* A website with **no plan** is refused with `publish it to see it`. Static files
  are not a process, so there is nothing to run, and framing the published site is
  the behaviour the preview replaced.

## The pipeline: a website

```
model writes src/App.tsx  ("Build It", repeatedly — this is not one-shot)
   │
   ├─ Publish It → runner materialises the files into builds/<slug>/
   │      │
   │      ├─ scripts/package-website.py <slug> --publish
   │      │      → builds/<slug>/dist/            what you serve
   │      │      → builds/<slug>/site.zip         source + dist + the Vite project
   │      │      → builds/<slug>/site.manifest.json
   │      │      → $OLYMPUS_SITES_ROOT/<slug>/    staged for the server
   │      │
   │      └─ scripts/studio-sites.py --publish <slug>
   │             → one NPM proxy host: <slug>.<suffix> → this host:SITE_PORT
   │
   └─ https://<slug>.<SITE_HOST_SUFFIX>/          live, wildcard TLS
```

`make sites-up` runs the server that serves both kinds. It selects a static site by
**hostname** — the leftmost label of `<slug>.<suffix>` is the directory — and falls
back to the path form `/<slug>/` for anything arriving by address rather than by
name. That is why the edge needs only a plain forward and no path rewriting.

## The pipeline: an application

An app is the same shape with two more steps, and the reason is that generating it
is not enough to have it: there is a client to build, a server to run and a
database to keep.

```
model writes src/App.tsx + server/schema.sql   ("Build It", repeatedly)
   │
   ├─ Publish It → runner materialises the files into builds/<slug>/
   │      │
   │      ├─ scripts/package-app.py <slug>
   │      │      → builds/<slug>/dist/client/          the built client
   │      │      → builds/<slug>/server/main.ts        the generated API
   │      │      → builds/<slug>/app.zip               client + server + Dockerfile
   │      │      → builds/<slug>/app.manifest.json
   │      │
   │      ├─ scripts/app-runtime.py --up <slug> --build
   │      │      → image olympus-app-<slug>:latest
   │      │      → container olympus-app-<slug>, 127.0.0.1:<port> → 3000
   │      │      → /var/lib/olympus/apps/data/<slug>/   the SQLite file
   │      │      → /var/lib/olympus/apps/nginx/<slug>.conf
   │      │      → nginx -s reload on olympus-sites
   │      │
   │      └─ scripts/studio-sites.py --publish <slug>
   │             → one NPM proxy host, exactly as a website gets
   │
   └─ https://<slug>.<SITE_HOST_SUFFIX>/ → olympus-sites → 127.0.0.1:<port>
```

### What the model writes, and what it cannot

The packager owns `package.json`, `vite.config.ts`, `tsconfig.json`, `index.html`,
`src/main.tsx`, `server/main.ts` and the `Dockerfile` — byte-for-byte from
constants, for the same reason the website scaffold is generated: a model-chosen
dependency range turns "it builds" into a coin flip.

`server/main.ts` is generated because it is the **request path**, and that is the
one place in a generated application where a mistake is not cosmetic. The model
writes the data model instead — `server/schema.sql` — and the server derives a JSON
REST API from the tables in it:

```
GET    /api/health
GET    /api/<table>?limit=&offset=&order=&<column>=...
POST   /api/<table>
GET    /api/<table>/<id>
PATCH  /api/<table>/<id>
DELETE /api/<table>/<id>
```

Table and column names are matched against the live schema before they are quoted
into SQL, so a request cannot name something that is not there — a 404 for an
unknown table, a 400 for an unknown field rather than a silently dropped one.

### One container per app, one port nobody sees

Each app gets its own image, container, port and SQLite file, so two apps cannot
collide over a table name or a connection. The port is published to **loopback
only**:

```
127.0.0.1:21400  →  olympus-app-weight-tracker:3000
```

`olympus-sites` runs with `network_mode: host`, so it — and therefore the edge —
reaches `127.0.0.1:<port>`. Nothing on the LAN does. The only way in is by name
through the edge, which is where the TLS and the identities live.

The name is routed by a **generated nginx vhost**, not by a port at the edge:
`app-runtime.py` writes `<slug>.conf` with an exact `server_name`, and an exact name
beats the static template's regex — so an app's name reaches its container while
every other name still falls through to the static tree. This is what keeps
`studio-sites.py --publish <slug>` identical for both kinds: the edge hears one port
for everything, forever.

A preview adds a **second** vhost for the same container — `<slug>-preview.conf`,
written by `app-runtime.py --up --preview` — so the project answers on two names and
neither the edge nor the container has to change to add the second one. The two are
separate files on purpose: removing a preview must not take the project's own name
with it.

```bash
make app-package SLUG=weight-tracker      # client build + archive
make app-up      SLUG=weight-tracker      # image, container, vhost
make app-publish SLUG=weight-tracker      # both, then the name
make apps-list
make app-down    SLUG=weight-tracker      # stop it, keep the database
make app-remove  SLUG=weight-tracker      # stop it, delete the database and image
```

Data lives in `/var/lib/olympus/apps/data/<slug>/app.sqlite` on the host, not in the
container, so a rebuild keeps it. `make app-down` keeps it too — only `app-remove`
deletes it, and that is the one command here that cannot be undone.

### The plan's data target: a container's own database, or Convex

Everything above describes the default target, `container`: the app keeps its own
database, on disk, in its own container. The planner may instead answer `convex`,
which means the state is served by the self-hosted Convex that **Atlas** runs — the
schema and the functions deploy there, and the client talks to it over HTTP and a
WebSocket.

It is **not** a third kind and not a second builder. A Convex-targeted project is
still built, packaged, run and published exactly as above — the plan's language,
commands, port and healthcheck are unchanged, and the container hosts the client.
What changes is where the data is, and there are three consequences:

* **Packaging needs the deployment's address.** `scripts/package-project.py` reads
  `CONVEX_URL` from the build environment and writes it into the image under the
  three names clients look for — `CONVEX_URL`, `VITE_CONVEX_URL` and
  `NEXT_PUBLIC_CONVEX_URL` — because Vite and Next inline these at bundle time, so
  handing it to the container at boot would be too late for a bundled client. A
  Convex-targeted plan with no URL is **refused** rather than packaged, because the
  alternative is a container that serves a client whose every read comes back blank.
* **The address is public, the credential is not.** The deployment URL is what the
  browser talks to, so baking it is the point. The one credential a build may use is
  `CONVEX_DEPLOY_KEY`, and only a key scoped to that one deployment: packaging passes
  it as a docker `--build-arg` (so it is never an `ENV` and never lands in the
  published image) and the Dockerfile declares it as an `ARG`. Docker records build
  arguments in the image metadata, which is why the platform admin key —
  `CONVEX_SELF_HOSTED_ADMIN_KEY` on Atlas — is never read here.
* **A static site cannot target it.** `static` is nginx serving files: there is no
  runtime that could deploy a function, so a plan that pairs the two is refused with
  the reason rather than built into a site whose schema never landed.

`build-runner.py` carries both `CONVEX_URL` and `CONVEX_DEPLOY_KEY` through to a
build by **exact name** — a `CONVEX_` prefix would also carry the backend's admin
key, which packaging is written never to read.

### One wildcard, then instant

Publishing a name used to mean a DNS record and its own Let's Encrypt order: a
minute or more per site, which is not a button you can put in a UI. `make
sites-wildcard` runs that slow half **once** — `*.studio.olympus.innotel.us` plus a
single certificate covering every name under it. After that a publish is one NPM
host, and it completes in seconds.

```bash
make sites-wildcard        # once per deployment
make sites-up              # the static server
make site-publish SLUG=todo-list
make sites-list
make site-check HOST=todo-list.studio.olympus.innotel.us
make site-unpublish SLUG=todo-list
```

`make site-check HOST=<name>` follows redirects, because a name behind the identity
provider (`studio.olympus.innotel.us`, `gateway.olympus.innotel.us`) answers `307`/`302`
by design: it reports *auth-gated — redirects to <the IdP>* rather than `FAILED`, and
still fails a name that redirects anywhere else, answers `200` with something that is
not a page, or does not answer at all. `python3 scripts/site-check.py --json` for the
raw hop.

`make site-unpublish` deletes the NPM proxy host, which is the only thing that makes
a name answer. The record stays — the wildcard already covers it — and the staged
files stay on disk, so re-publishing is instant. What a client gets afterwards is
not a 404: with no host bound to the name, no certificate is presented for it
either, and the handshake fails with `unrecognized name`. That is the name being
removed rather than a fault in the edge.

The name is **derived, never typed**: it is always `<slug>.<SITE_HOST_SUFFIX>`, the
same slug the build directory uses. A free-form name would be a second namespace to
keep in sync with the first, and the requests that go wrong would go wrong
silently — pointing at another site's host.

`cerulean_api.select_certificate` prefers a **dedicated** certificate over a
wildcard when a name has one, so a site that later needs its own certificate can
have one without this path fighting it. A wildcard covers exactly one label:
`*.studio.olympus.innotel.us` covers `todo.studio.olympus.innotel.us` and neither
`studio.olympus.innotel.us` nor `a.b.studio.olympus.innotel.us`.

### The name hash has to fit the longest name

nginx hashes every exact `server_name` when it starts, and it refuses to reload at
all when one does not fit — `could not build server_names_hash, you should increase
server_names_hash_bucket_size`. The default is 32/64 and a name here is long by
construction: a slug of up to 60 characters, `-preview` for a preview, and the
suffix. Because the failure is on the whole hash rather than on one vhost, **one
over-long name stops every publish**, including ones already working.

`deploy/nginx-sites.conf.template` sets `server_names_hash_bucket_size 128;`, which
covers the longest name the slug limits allow. It is an http-level directive, so it
lives at the top of that template — which the nginx image renders into
`/etc/nginx/conf.d/`, and that directory is included *inside* the `http` block. A
`server` block would reject it. Changing the template needs the container
recreated, not reloaded: the rendered file is written at start.

### A reload the edge refuses is not a lost build

If `nginx -t` rejects the vhost just written, `scripts/app-runtime.py --up` takes **that
file back out** — only the one this run created — asks the edge to reload again, and
fails naming the container it left running. It does not remove the container, and that is
the point: nginx keeps serving its last good config, so the app is up on its loopback
port and no other site on the edge was ever affected. Removing it would turn a routing
fault into a build you have to run again. A vhost that was already there is left in
place, so a failed reload cannot drop a name that was working before the call.

A reload that fails *after* `nginx -t` accepted the config is reported the same way —
container left running, retry named — with the promise that the name starts working at
the next reload, because the config on disk is valid.

### `Technitium session expired` is not a retry, whatever it says

Every publish reads the zone's records first, through Cerulean, which talks to Technitium.
When Technitium answers `invalid-token`, Cerulean's message ends in *"— retry"*, and that
instruction used to be the one thing that could not work: a **configured** static token
was used as-is with no re-login, so every DNS read failed until somebody unset it. On
2026-09-13 that was **five hours** of identical failures — measured, from
`2026-09-13T20:46Z` until the token was replaced — and every publish in that window died
on it.

Two changes came out of that, because either alone leaves the hole open:

* `cerulean_api.unusable_dns_session` detects that particular 500 and replaces "retry"
  with what actually clears it, so a repeat costs minutes rather than an afternoon.
* In the platform's own checkout, `server/src/services/technitium.ts` falls back to the
  `TECHNITIUM_USER`/`TECHNITIUM_PASSWORD` login when a configured token is rejected, and
  warns once about the token. A dead token becomes one extra round trip per call instead
  of an outage — verified against the live Technitium with a deliberately dead token,
  which read all six zones.

So this 500 now means *both* credentials were refused, which is why the message names
both.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `OLYMPUS_SITES_ROOT` | `/var/lib/olympus/sites` | Staged sites on the host; mounted into `olympus-sites` |
| `OLYMPUS_APPS_ROOT` | `/var/lib/olympus/apps` | App runtime state: `runtime/`, `nginx/`, `data/`. The `nginx/` half is mounted into `olympus-sites` at `/etc/nginx/app-conf.d`, so it has to exist before `make sites-up` |
| `APP_PORT_BASE` | `21400` | The first loopback port an app may be given |
| `APP_PORT_RANGE` | `200` | How many to try before refusing. An app keeps the port it was first given, and the number is only reused once its record is gone |
| `SITE_PORT` | `20130` | The static server's port — **also** the port a published site forwards to |
| `SITE_HOST_SUFFIX` | `studio.olympus.innotel.us` | The domain names are built from: `<slug>.<suffix>` |
| `SITE_EDGE_FORWARD_HOST` | *(empty)* | This host's LAN address. Empty makes publishing refuse, rather than put a name on the edge that answers nothing |
| `SITE_CERT_RENEW_DAYS` | `30` | A certificate is reused only if it lasts this long |
| `STUDIO_BUILDS_DIR` | `/app/builds` | Read-only mount Studio serves `site.zip` from |
| `CONVEX_URL` | *(empty)* | The self-hosted Convex deployment (Atlas) a Convex-targeted plan's client is pointed at. Empty means no plan may target Convex; packaging refuses one rather than shipping a client that reaches nothing |
| `CONVEX_DEPLOY_KEY` | *(empty)* | Optional, and only for a plan whose own build step deploys the functions. Must be scoped to that one deployment — it is passed as a build argument, so Docker records it in the image metadata |

The staged tree is **outside the checkout** on purpose: served content is runtime
state, and a served tree inside the repo is one `git add -A` away from being
committed. The nginx config is a template (`deploy/nginx-sites.conf.template`)
rendered by the image's own envsubst with
`NGINX_ENVSUBST_FILTER=SITE_PORT|SITE_HOST_SUFFIX` — those two substitutions and
no more, so nginx's own `$uri` and `$site` survive. Application vhosts are **not** in
that template: they are separate files under `/etc/nginx/app-conf.d` (a bind mount
of `$OLYMPUS_APPS_ROOT/nginx`) pulled in by one `include`, because they change at
runtime and a rendered template does not.

Because `SITE_PORT` and `SITE_HOST_SUFFIX` are read by the server, the publisher and
the nginx template alike, the edge, the listener and the vhost cannot disagree
about where a site answers.

## What is deliberately not automated

**Nothing is authenticated on the site server.** It is a plain static file server
with no login: anything that can route to the host can read any staged site. That
is the intent — a published site is meant to be public — and it is why the port is
not the site's only control. If a site has to be private, do not publish it; the
zip download is the delivery path for that.

**The server is not in the default compose profile.** `make sites-up` opts in. A
stack that has never published a site or an app should not be running a web server.

**No clean-up.** Republishing replaces a slug's directory; nothing removes a site
you have stopped publishing. The DNS record, the certificate and the NPM host stay
until they are removed in Cerulean, which is the platform that owns them.

**App containers are not garbage-collected either.** `docker run --restart
unless-stopped` means a published app comes back after a reboot and keeps coming
back until `make app-down`. Nothing sweeps a container whose app you stopped using,
because "stopped using" is not a thing the host can tell — the container idles at
almost no cost.

**An app is not behind Studio's identity.** The name resolves through the same
trusted wildcard as a website, and the app's own vhost does not add an SSO gate.
An app that needs a login has to bring its own — that is a real limitation, not a
missing flag, and it is stated here so it is not discovered by publishing one.

## ONYX (Online Storage System) integration — ON HOLD

> **Status: on hold, not being pursued.** Nothing below is wired up, and nothing
> in this repo calls ONYX. It is kept because the research is real and the
> integration points are still the right ones whenever it is picked up — not as a
> plan in progress. The current publishing path is entirely local: staged files on
> this host, served by `olympus-sites`, published through Cerulean + NPM.

ONYX is the platform that owns storage on this network — read the design docs at
<https://innotelinc.github.io/onyx/>. It would be relevant here because a published
site is two things this stack does not own: **files that want to live on the NAS**,
and **a name that wants to be a first-class ONYX surface** rather than a one-off.

What ONYX gives us, as documented:

| Surface | Address | Use |
|---|---|---|
| S3-compatible object storage | `storage.onyx.innotel.us` | Site archives and object-per-site storage |
| App hosting | `app.onyx.innotel.us` | The intended home for served apps and sites |
| NAS shares | `bin/onyx share create …` over SMB/NFS/FTP/SFTP/WebDAV/Rsync | The staged tree itself — pool path `/mnt/onyx/pool1/@data/...` |
| Backups | `backup.onyx.innotel.us` | Snapshot and backup jobs for whatever holds the sites |
| Identity | `auth.onyx.innotel.us` (Authentik) | SSO for anything that needs a login |
| Edge | Nginx Proxy Manager, wildcard Let's Encrypt | The same NPM instance this stack already publishes through |

### The three integration points, in the order they are worth doing

**1. Put the staged tree on an ONYX share.** Today `OLYMPUS_SITES_ROOT` is a local
directory. ONYX's whole argument is that this directory should be a share — Btrfs
with checksums and snapshots, exposed once and available over six protocols:

```bash
bin/onyx share create sites /mnt/onyx/pool1/@data/sites --smb --nfs
```

Then point this stack at the mounted path:

```bash
OLYMPUS_SITES_ROOT=/mnt/onyx/pool1/@data/sites
```

nothing else changes. This is the highest-value step by a wide margin: it is what
turns "a directory on the olympus host" into "storage with snapshots, scrub and a
restore path", which is the property ONYX exists to provide. It also makes the
sites readable from any machine on the network over SMB, without a publish step.

The same argument applies with more force to `OLYMPUS_APPS_ROOT/data/`, which holds
each app's SQLite file. A site can be rebuilt from its spec; a database that someone
has been entering a weight into every morning cannot. If only one of the two is put
on the NAS, it should be that one.

**2. Serve from ONYX app hosting instead of this stack's nginx.** ONYX's `v0.4
"Jade"` milestone is apps — an app store, sandboxing and Docker integration — and
`app.onyx.innotel.us` is the surface. When it lands, `olympus-sites` becomes
redundant: the staged tree is already on a share, so ONYX serves the same bytes
from the same place, with SSO and wildcard TLS it already owns. Until then, this
stack's nginx is a stopgap and should be treated as one.

**3. Mirror archives into ONYX object storage.** `dist/` is small and static, which
makes it a natural S3 tenant — one object per published build, which is a versioned
history of the site for the cost of nothing:

```bash
aws --endpoint-url https://storage.onyx.innotel.us s3 sync \
    /var/lib/olympus/sites/<slug> s3://sites/<slug>/ --delete
```

This is also the answer to *rollback*: the local staging directory holds one
version, and object storage holds all of them.

### Why this is parked

Steps 1–3 need one thing this checkout cannot prove it has: ONYX itself answering.
`/mnt/onyx` exists on the Cerulean host but is empty (`drwxr-xr-x 2 root root 2`),
no `onyx-*` container is running, and `<name>.onyx.innotel.us` names are published
by the platform's own tooling. Writing an `aws s3 sync` into a Makefile whose
endpoint answers nothing would be a target that fails quietly, which is worse than
one that is written down and honest.

The order above is also the dependency order — object storage mirroring is only
worth writing once the tree it mirrors lives on the NAS.

## Related

- `docs/stack.md` — where this fits in the stack's roles
- `docs/gateway-sso.md` — the same Cerulean + NPM edge path, for the gateway
- `.archon/workflows/app/greenfield/` — the workflow that writes the source
