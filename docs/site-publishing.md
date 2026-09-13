# Publishing a Studio website

Studio builds two kinds of thing, and only one of them is finished when it is
generated.

| | **App** | **Website** |
|---|---|---|
| What it is | One self-contained page — HTML, CSS, JS | A Vite + React 19 + TypeScript project |
| Preview | Runs live in the sandboxed iframe as it streams | None until it is built; JSX needs a compiler |
| Finished when | It has been generated | `dist/` exists |
| Delivered as | A saved library entry, a zip, a factory spec | `dist/` on a name, or a zip of source + `dist/` |

Nothing about this is a preference. A React site is a build artifact, and the only
honest place to say so is here.

## The pipeline

```
model writes src/App.tsx
   │
   ├─ scripts/package-website.py <slug>          scaffold + npm ci + vite build
   │      → builds/<slug>/dist/                  what you serve
   │      → builds/<slug>/site.zip               source + dist, for handing over
   │      → builds/<slug>/site.manifest.json     what was built, and when
   │
   ├─ scripts/package-website.py <slug> --publish
   │      → $OLYMPUS_SITES_ROOT/<slug>/          a copy of dist/, staged to serve
   │
   ├─ make sites-up                              nginx serves the staged tree
   │      → http://<this host>:$SITE_PORT/<slug>/
   │
   └─ make site-publish SLUG=<slug> HOST=<name>  Cerulean DNS + cert + NPM host
          → https://<name>/                       the published site
```

Four steps, and each one is useful on its own. That is deliberate: packaging needs
no configuration and is testable offline, while publishing needs Cerulean and the
edge, and a failure there must not discard a good build.

### From Studio

**Build & publish** on a website does the first two steps through the host-side
build runner (`make app`, then packaging). **Download .zip** saves the archive —
the packaged one when there is one, the source otherwise. Export to factory writes
a spec whose *Next steps* section names the packaging command, because a spec that
stopped at `make app` would hand the factory source that nothing can open.

### From a shell

```bash
make site-package SLUG=todo-list          # build + stage (needs the build to exist)
make sites-up                             # start the static server
make site-publish SLUG=todo-list HOST=notes.sites.innotel.us
make site-check HOST=notes.sites.innotel.us
```

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `OLYMPUS_SITES_ROOT` | `/var/lib/olympus/sites` | Staged sites on the host; mounted into `olympus-sites` |
| `SITE_PORT` | `20130` | The static server's port — **also** the port `make site-publish` registers at the edge |
| `SITE_EDGE_FORWARD_HOST` | *(empty)* | This host's LAN address. Empty makes `site-publish` refuse, rather than publish a name that answers nothing |
| `SITE_HOST_SUFFIX` | `sites.innotel.us` | Default domain for `HOST` |
| `STUDIO_BUILDS_DIR` | `/app/builds` | Read-only mount Studio serves `site.zip` from |

The staged tree is **outside the checkout** on purpose: served content is runtime
state, and a served tree inside the repo is one `git add -A` away from being
committed. The nginx config is a template (`deploy/nginx-sites.conf.template`)
rendered by the image's own envsubst with `NGINX_ENVSUBST_FILTER=SITE_PORT` — one
substitution, so nginx's `$uri` in `try_files` survives.

Because `SITE_PORT` is read by both the server and the publish target, the edge and
the listener cannot disagree about where a site answers.

## What is deliberately not automated

**Nothing is authenticated on the site server.** It is a plain static file server
with no login: anything that can route to the host can read any staged site. That
is the intent — a published site is meant to be public — and it is why the port is
not the site's only control. If a site has to be private, do not publish it; the
zip download is the delivery path for that.

**The static server is not in the default compose profile.** `make sites-up` opts
in. A stack that has never published a site should not be running a web server.

**No clean-up.** Republishing replaces a slug's directory; nothing removes a site
you have stopped publishing. The DNS record, the certificate and the NPM host stay
until they are removed in Cerulean, which is the platform that owns them.

## ONYX (Online Storage System) integration

ONYX is the platform that owns storage on this network — read the design docs at
<https://innotelinc.github.io/onyx/>. It is relevant here because a published site
is two things this stack does not own: **files that want to live on the NAS**, and
**a name that wants to be a first-class ONYX surface** rather than a one-off.

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

### Why this is notes and not code

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
