# Olympus — Platform Stack Role

**Classification: FactoryOps**

**Role: FactoryOps — issue → validated pull request automation for the repository it lives in.**

Olympus is a repository-local AI software factory. It turns accepted GitHub issues into validated pull requests through automated triage, planning, implementation, independent validation, safety gates, and controlled merging — observable at autonomy level 0 before any unattended operation. This page declares its role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) — the canonical single-responsibility architecture. The stack is defined in exactly one place; this page links this product to it and states what it owns, consumes, provides, and explicitly does not own.

> **Convergence:** see the [**build-plane convergence plan**](https://github.com/innotelinc/innotel-platform-stack/blob/main/docs/convergence-onyx-olympus-distro-atlas.md)
> — Olympus is the target home for the shared builder: Studio is the one web UI,
> `scripts/olympus-tui.py` the one terminal UI, and the plan → runner → package →
> runtime path the one engine. It also retires its `gateway` profile for the one
> shared OmniRoute (§4.4) and absorbs Distro's control plane as the builder's
> tenancy layer (§5).

## Owns

- Repository automation — issue, branch, PR, workflow, and autonomy state machine; accepted issue → watched Archon workflow → open PR
- Validation and safety — protected-path enforcement, required markers, non-zero evidence counts, ratchet floors, stop controls, bounded changes
- Operational visibility — setup blockers, configured autonomy, earned evidence, in-flight work, held work, and verification results (fail-closed)
- The five Archon factory workflows (prime, implement, validate, regress, triage) and the harness as the definition of “working”
- Fast lane (Issue → Fix PR): ~35 min wall-clock per issue including autonomous code review
- Local CPU posture — 100% of heavy model inference outsourced to free cloud models via the OmniRoute gateway (Codex `auto/coding` + Hermes 3), local Ollama bypassed

## Provides

- An agent-ready clone: `bash scripts/bootstrap.sh` installs the OmniRoute, Codex (primary coding brain, `wire_api = "responses"`), Claude Code, and Archon CLIs, wires agents to the gateway, mints an API key, and reports `factory/doctor.py`
- Deterministic gating/merging after independent validation; mutation checks and holdout contract for auto-merge quality
- The “teammate” SDLC loop (Archon DAG + factory consumer) that can be triggered from Telegram via Hermes 3

## Consumes

- OmniRoute — model gateway (Codex `auto/coding` + Hermes 3 via OpenRouter/DeepInfra)
- Authentik — identity, SSO (Cerulean's Authentik; optional: local/OIDC mode where applicable)
- Cerulean Vault — secrets, credentials (gateway keys, tokens)
- Cerulean — trust (DNS/TLS for the operator surfaces where exposed)
- NPM Edge — public routing, TLS termination at the edge (where the operating host is fronted)

## Explicitly does NOT own

- Application business logic — Olympus is factory machinery, not a product domain; it does not ship a user app, media library, billing products, or storage promises
- Identity (Authentik), Secrets (Cerulean Vault), Trust/DNS/PKI (Cerulean), Storage (ONYX), or Revenue/Billing (Magnate) — the factory integrates with those instead of re-implementing them
- Its own governance as ordinary issue work — `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`, `factory/**`, `.archon/workflows/factory/**`, `harness/**`, `.factory/locks/**`, `.factory/holdout/**`, CI config, and secret-shaped files are protected

## Service map

| Component | Technology | Job |
|---|---|---|
| `factory/` machinery | Python (gate, guard, merge, state, doctor, trigger) | State machine, safety gates, merging, visibility, scheduler control |
| `harness/` | `ci.py`, `harness.config.json`, journeys, holdout | Definition of “working” — never edited to make a check pass |
| `.archon/workflows/factory/` | Archon YAML workflows | Prime → implement → validate, plus regress and triage |
| `scripts/bootstrap.sh` + `scripts/omniroute-vault.sh` | bash + curl + OmniRoute CLI | One-command clone-to-ready and Vault-backed gateway launcher |
| Telegram interface | Hermes 3 via OpenRouter Free through OmniRoute | Interactive bot that parses intent into Archon DAG runs |
| Coding brain | Codex (`auto/coding`) via OmniRoute Responses API (`wire_api = "responses"`) | Repository code modifications dispatched by the factory consumer + harness E2E (`omniroute launch-codex -p auto-coding`) |
| `web/studio/` | Next.js (App Router) + Authentik OIDC + Distro's control plane for tenancy | Browser vibe-coding surface — prompt in, app out, gateway key held server-side; saved apps are scoped per identity on the stack's own volume. Two kinds: an **app** is full-stack (React + Node API + SQLite), a **website** is static React. With `CONTROL_PLANE_INTERNAL_URL` + `CONTROL_INTERNAL_TOKEN` set, each turn is spent on the signed-in user's **own** gateway key with a quota check before it and usage recorded after it, and the library is keyed on the control-plane user id (an existing subject-keyed library is adopted in place); with them empty Studio spends the shared key, single-operator |
| Build runner (`scripts/build-runner.py`) | Python + systemd (`olympus-build-runner.service`); account probed at install | Executes `make app` for builds Studio queues. Studio's image carries no toolchain, so the queue file is the whole interface — and the runner treats it as untrusted input. Every build is then packaged, because `make app` ends with source: `scripts/package-app.py` for an app, `scripts/package-website.py` for a website |
| App packaging (`scripts/package-app.py`) | Python + npm + Vite | Writes the project a generated app is dropped into — Vite config, `package.json`, the **generated** `server/main.ts` and the `Dockerfile` — then builds the client. The model writes only `src/App.tsx` and `server/schema.sql`; the server derives its REST API from those tables, so the model never writes the request path |
| App runtime (`scripts/app-runtime.py`) | Python + docker | One container, one loopback port and one SQLite file per app, under `OLYMPUS_APPS_ROOT`. Also writes the app's nginx vhost and reloads `olympus-sites`, which is how a name reaches a container without the edge learning a port |
| Sites and apps edge (`docker-compose.yml`, profile `sites`) | nginx (`olympus-sites`), host-networked on `SITE_PORT` | Serves the staged tree `scripts/package-website.py --publish` writes under `OLYMPUS_SITES_ROOT`, and reverse-proxies each running app to its container by hostname. `make site-publish`/`make app-publish` then put the name on through Cerulean + NPM. No auth by design — a published site or app is public, and the zip download is the delivery path for anything that is not. See [site-publishing.md](site-publishing.md) |
| Gateway dashboard SSO (`compose.gateway-sso.yml`) | oauth2-proxy + Authentik (`olympus-gateway-sso`) | The gateway dashboard manages provider credentials, so it is fronted by an identity-aware proxy restricted to one Authentik group. OmniRoute's own OIDC cannot be used: its callback strips the trailing slash Authentik always puts in `iss` and then demands an exact match — see [gateway-sso.md](gateway-sso.md) |
| Gateway auth mode (`scripts/gateway-auth-mode.py`) | Python, no dependencies | Turns the gateway's *own* login off so Authentik is the only gate: one login for one surface, and the half that cannot authenticate anyone is gone. `requireLogin=false` makes reachability of `20128` the entire control, so the script checks the binding first and refuses otherwise — "local" being loopback plus the host's own docker0 gateway, which only containers on that host can dial, and never a LAN address. `make gateway-auth-mode`, `--dry-run`, `--verify`, `--container` |
| Gateway sessions (`compose.gateway-sso.yml`) | redis 7 (`olympus-gateway-sso-sessions`), loopback `127.0.0.1:16379` | Server-side sessions for the proxy above. A cookie session carries every group the identity claims; thirty groups overflow the 4KB limit, the split `Set-Cookie` headers exceed the edge's proxy buffer, and the login callback gets a 502 from the edge. No volume: losing it costs a re-login, nothing else |
| Gateway name check (`scripts/gateway-edge-check.py`) | Python, no dependencies | Walks `gateway.olympus.innotel.us` link by link — DNS at two resolvers, TLS, the edge, the SSO proxy, the session store — and names the first broken one, because a dead edge, a lapsed certificate and a stopped proxy all reach a person as "it's not resolving". The session link is the one a `curl` cannot reach, since the failure only exists after a login. `make gateway-edge-check`; daily under `olympus-gateway-edge-check.timer` |
| `scripts/cerulean-edge.py` | Python + the Cerulean API | Publishes a public name the way Cerulean intends: DNS record, certificate, and the NPM proxy host. The record is created in Technitium, the certificate by Cerulean's ACME job, and the host through Cerulean's NPM service — so the audit trail is Cerulean's, not a side door into Technitium |
| `scripts/omniroute-restore-providers.py` | Python + the OmniRoute CLI | Copies provider credentials from an existing OmniRoute data dir into the gateway that is serving traffic — the repair for a gateway that came back with no connections |
| `scripts/prune-builds.py` | Python | Reports (or removes) old `builds/<slug>` directories and finished queue files, so a slug can be rebuilt past the clobber guard |

> **The build runner's account is a platform capability, not a preference.** A build
> runs a coding agent inside Codex's bubblewrap sandbox, and bubblewrap needs user
> namespaces. Container environments commonly deny those to unprivileged users, and
> this one does (`unshare -U` as a plain user: `Permission denied`, while root can).
> There, a non-root runner does not make builds safer — it makes the sandbox
> unavailable, so the agent executes unsandboxed with whatever that account can read
> (including `.env`, which holds the Vault and Authentik tokens).
>
> So `scripts/install-build-runner.sh` probes with the build account and installs the
> configuration that keeps the agent sandboxed: `olympus-builder` where user
> namespaces work, root where they do not, printing which it chose and why.
> `--as-user` / `--as-root` override it. Where the account *is* the runner's, the
> installer hands over `builds/`, the shared queue (group + an ACL for Studio's uid
> 1001) and an ACL on `.env`; where it is root, none of that is needed and the
> sandbox is what confines the agent. Codex's Landlock fallback is not an escape
> hatch here: this build calls bubblewrap the default and Landlock the legacy path,
> and it refuses the fallback for the `workspace-write` profile.

> **The gateway is a shared platform service, not part of this stack.** The
> platform's single OmniRoute runs in Group 2 (`2-voice/`, mesh `10.10.2.1`,
> Consul service `omniroute`) on the published `diegosouzapw/omniroute` image,
> with its state in that group's `omniroute-data` volume. This stack used to keep
> a copy of its own — the `omniroute` service behind `profiles: ['gateway']`,
> started by `make gateway-up` — and it was removed once the shared gateway held
> the provider connections; see
> `ips/docs/convergence-onyx-olympus-distro-atlas.md` §4.4. What the local copy
> taught this deployment is worth keeping, because it is equally true of the
> shared one. Two consequences are worth knowing before you touch it.
>
> * **`server.env` in that volume is the key to everything else in it.** It holds
>   `STORAGE_ENCRYPTION_KEY` (and `API_KEY_SECRET`), which is what decrypts the
>   provider connections stored in the `storage.sqlite` beside it. A volume copied
>   with that file intact keeps working — verified by comparing all ten connections
>   by id after the move; a volume rebuilt without it comes back with credentials
>   that are not *missing* but unreadable. `make gateway-vault-backup` puts both
>   halves in Cerulean Vault under the gateway host's own path —
>   `make gateway-vault-backup` / `-check` are run on that host, because the
>   script asks Docker for the gateway container's mount — and
>   `make gateway-vault-check` exits non-zero when that copy has drifted from the
>   live gateway, so it is worth a timer. The restore was exercised against a copy
>   of the live database: the key read back from Vault decrypted all ten
>   connections, which is the property the backup exists to have.
> * **Whoever talks to the gateway inherits the topology problem.** `OMNIROUTE_BASE_URL`
>   is one value in one `.env`, and host-side scripts read that same value, so a
>   container cannot be handed a different one. From any other host it is
>   `http://192.168.1.46:20129/v1` — the gateway host's LAN address, and the
>   Authentik SSO proxy in front of the gateway, which exempts `/v1` for API
>   clients. The gateway's own `:20128` is published on that host's loopback and
>   bridge alone, so it is not a target at all. On the gateway's own host a
>   container uses `http://host.docker.internal:20129/v1`, and a host-mode caller
>   `http://127.0.0.1:20129/v1` — every service that talks to it then runs with
>   `compose.host-gateway.yml` — Studio included
>   (`make docker-studio-up`). A Studio
>   rebuilt without that override resolves `127.0.0.1:20128` to *itself*, answers
>   `ECONNREFUSED` to its own requests, and stays `healthy` the whole time, because
>   its healthcheck only asks whether Studio answers.
>
> **The gateway's provider connections are deployment state, not code.** OmniRoute
> keeps them in the `storage.sqlite` of the data dir it was started with. Replace the
> data dir — a container recreated on a fresh volume, or a move from a host-run server
> to a container — and the gateway comes back with **zero** connections while still
> answering on its free providers. Nothing looks broken, but `auto/coding` (a *combo*)
> has no credentialed member to pick: it pins its last-known-good to a provider with no
> credentials and every turn after the first answers `503 No credentials for opencode`.
>
> That is what happened on this deployment: `127.0.0.1:20128` serves from a container
> volume with no connections, while the credentials the operator had configured sat in
> a host-run instance's dir (`/root/.omniroute`) that the container replaced. Fix it
> with:
>
> ```bash
> scripts/omniroute-restore-providers.py --dry-run      # what would be copied
> scripts/omniroute-restore-providers.py                # copy them, and clear the pin
> ```
>
> The script decrypts locally with `omniroute auth export` (the credentials are still
> in the old dir, encrypted with *its* key), authenticates to the target with its
> dashboard password (`OMNIROUTE_DASHBOARD_PASSWORD`) or a management-scoped key, adds
> only what is missing, and then clears the combo's last-known-good via
> `DELETE /api/settings/lkgp-cache`. That last step is load-bearing: the pin survives
> adding connections, so without it the gateway keeps routing to the provider that 503s
> and the restore looks like it did nothing. OAuth connections (github) need their own
> `omniroute providers auth <provider>` flow and free ones need no credential at all;
> the script reports both as skipped rather than dropping them silently.
>
> **What the restore does NOT buy you.** Connections are not capacity. Of the ten accounts
> imported here, agentrouter returned `402 budget pool quota exhausted`, openrouter `401
> all connection(s) credits exhausted`, and openai `429 no credits remaining` — the gemini
> accounts were the only ones that answered, and their free tier cools down for 15–23
> minutes after a burst (`429 … all credentials cooling down`). Nine connections looked
> restored and none of them was a durable build model. Check the account, not the
> connection count:
>
> ```bash
> curl -s $OMNIROUTE_BASE_URL/responses -H "Authorization: Bearer $OMNIROUTE_API_KEY" \
>   -H 'Content-Type: application/json' \
>   -d '{"model":"gemini/gemini-3-flash-preview","input":"make a file with the shell tool",
>        "tools":[{"type":"function","name":"shell","parameters":{"type":"object",
>        "properties":{"command":{"type":"string"}},"required":["command"]}}]}'
> ```
>
> A 200 is not the whole check either — the response has to contain a `function_call`.
> Two measured ways a model "succeeds" while building nothing:
>
> * `oc/big-pickle`, the free member `auto/coding` prefers, returns 200 and writes the
>   file contents out as *chat text*. The app directory stays empty, the agent looks busy
>   for minutes, and there is no error message — the reason the build model is pinned in
>   `.env` rather than left to the auto policy, which walks the whole catalogue (`Trying
>   model 1109/1388`) and so decides the outcome by luck.
> * `gemini/gemini-2.5-flash` answers a hand-made tool request and returns `400 Function
>   calling config is set without function_declarations` under the tool payload Codex
>   actually sends. A gateway-side translation gap, not a repo bug — and the reason
>   `.env.example` names it as known-bad instead of leaving it to be rediscovered.
>
> The `.env` is also the *only* place that setting takes effect. Archon strips the repo's
> `.env` keys from a script node's environment and runs the workflow from a copy under
> `artifacts/runs/<id>/workflow-source/`, so a node reading only `os.environ`, or walking
> up from its own file, silently uses its code defaults — measured: a deployment pinned to
> `gemini/gemini-3-flash-preview` asked the gateway for `auto/coding`. `build-app.py` now
> reads the checkout `.env` located through the app directory it was handed.
>
> Two habits worth keeping for any agent work here: never read an agent's exit code as
> evidence it did anything (Codex exits **0** after a refused request), and if you do
> fall back to a concrete model name rather than the combo — naming a model keeps the
> turn off the combo path entirely (`OMNIROUTE_MODEL` / `OMNIROUTE_MODEL_FALLBACK` in
> `.env.example` are the app-builder's case of this). [build-model.md](build-model.md)
> covers the capacity side: which connections were measured as unusable, what
> `make build-model-check` proves and does not, and the options for a durable fix.

> **Scope of this checkout.** This repository is the deployment surface. The
> factory state machine, the harness, and the Archon workflow definitions
> (`factory/**` beyond `doctor.py`/`trigger.py`, `harness/**`,
> `.archon/workflows/factory/**`) live in the source repo, not here, and
> `core-modules/` is cloned on demand by `setup.sh`. Run
> `python3 factory/doctor.py` to see what this checkout has and what it is
> missing.

## In the ecosystem

| Flow | Path |
|---|---|
| Definition source | [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) is canonical; this page is the product's link to it |
| Identity | Cerulean's Authentik at `https://auth.cerulean.innotel.us`, aliased at `auth.olympus.innotel.us` — the same alias every platform gets — OIDC provider per service; the registration tooling authenticates as a least-privilege service account, never as an administrator |
| Secrets | Cerulean Vault (KV v2) — credentials live in Vault; `.env` carries `vault://` references and is gitignored |
| Trust / Edge | Cerulean (DNS/certs) and NPM Edge where the hosting host is fronted — managed by Cerulean |
| AI plane | OmniRoute gateway in front of upstream models — Codex via `wire_api = "responses"` (primary), one key per user |

## Integration with other platforms

- **Atlas (CodeOps)** — Olympus is the factory that Atlases the ecosystem's code lives in; Atlas owns the canonical git remote + CI for platform code, while Olympus owns the factory loop inside a given repo.
- **Distro (BuilderOps)** — the tenancy service for the builder: accounts, per-user gateway keys, quota gating and usage/audit. Its bolt.diy web front door retired in the build-plane convergence, so **Studio (this repo) is the one web UI** and it consumes Distro's control plane; a packaged project lands on an Atlas Gitea remote instead of being built in a browser. Studio, Distro and Atlas all sit behind OmniRoute + Authentik + Magnate. Olympus below that is the repo-bound automation that validates those outputs.
- **ONYX (Online Storage System)** — **on hold, deliberately.** Olympus does not own storage, and a published Studio website is files that want to live on the NAS, so the integration points are real; they are not being pursued now and nothing in this repo calls ONYX. `docs/site-publishing.md` keeps the research under an explicit on-hold heading rather than as a plan in progress. Publishing today is local: staged files on this host, served by `olympus-sites`, named through Cerulean + NPM.

## Secrets (Cerulean Vault)

Secrets for this platform live in **Cerulean Vault** — HashiCorp Vault with KV v2
mounted at `VAULT_PREFIX` (default `cerulean`). Olympus does **not** run its own
store; it consumes the platform's, so there is one place credentials live rather
than one per service.

The token is scoped to **this stack's own path**, not to the whole mount.
Cerulean's default `cerulean` policy grants `<prefix>/data/*`, which every
product sharing the mount can use to read and overwrite the others' secrets.
Olympus holds a narrower `olympus` policy instead:

| Policy | Grants | Reaches |
| --- | --- | --- |
| `cerulean` (platform default) | `<prefix>/data/*` | every product in the mount |
| `olympus` (used here) | `cerulean/data/olympus*` + its metadata | Olympus only |

Written with `vault policy write olympus -` (KV v2 addresses data and metadata
under separate paths, and there is deliberately no `list` on the mount root —
that would disclose every sibling key's name):

```hcl
path "cerulean/data/olympus"       { capabilities = ["create", "read", "update", "delete", "list"] }
path "cerulean/data/olympus/*"     { capabilities = ["create", "read", "update", "delete", "list"] }
path "cerulean/metadata/olympus"   { capabilities = ["read", "list"] }
path "cerulean/metadata/olympus/*" { capabilities = ["read", "list"] }
```

It is a *periodic* token, so a renewal resets its TTL to the full period (32
days) and it can live indefinitely — but only if something renews it inside that
window. `scripts/vault-renew.sh` is that renewer; `--check` reports without
renewing and exits non-zero once the token stops being renewable, so it is safe
to wire into monitoring. On the deployment host a systemd timer renews it daily
so nobody has to remember (`scripts/install-token-check-timer.sh` installs both
this stack's timers), and `scripts/vault-renew-alert.sh` wraps it with the same
Telegram alerting as the credential check — a renewal failure means every
`vault://` reference in `.env` stops resolving, so it is not left silent.

```bash
# Consume the platform Vault (Cerulean already runs it as `cerulean-vault`).
export VAULT_ADDR=http://vault:8200
export VAULT_TOKEN_FILE=./data/vault/token/olympus.token   # `olympus` policy, never root
export VAULT_PREFIX=cerulean
python3 scripts/vault-bootstrap.py

# Keep the periodic token from lapsing (or `make vault-renew`).
bash scripts/vault-renew.sh
```

The platform **mints and renews** that token: `cerulean-vault` writes
`data/vault/token/olympus.token` on the Vault host (named by
`VAULT_PRODUCT_TOKENS=olympus` there), which is the file to copy here when it is
created or re-minted. Because renewal keeps the value stable, a copy taken once
stays valid for as long as the platform keeps running — this side only needs to
renew it if the platform stops.

`scripts/vault-bootstrap.py` takes every address and credential from the
environment (nothing is hardcoded, because this repo is public), creates the KV
v2 mount if it is absent, refuses a KV **v1** mount rather than silently writing
unversioned data, and reads the secret back to confirm it round-trips before
reporting success. Values are never printed.

With `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_PREFIX` in `.env`, any env value may
be a `vault://<mount>/<path>#<key>` reference resolved at startup — the same
convention Cerulean uses, so the stack and the platform agree on one format:

    OMNIROUTE_INITIAL_PASSWORD=vault://cerulean/olympus#INITIAL_PASSWORD

`scripts/omniroute-vault.sh` resolves that secret with `curl` and starts the
gateway with it, so no Vault CLI has to exist on the host.

The Authentik registration credential lives beside that password, still inside
this stack's own path, and `.env` carries the reference rather than the value:

    AUTHENTIK_TOKEN=vault://cerulean/olympus/authentik#AUTHENTIK_TOKEN

`scripts/authentik-studio-app.py` resolves the reference before it calls
Authentik. The credential is a service account (`olympus-studio`), not an
administrator: the `olympus-studio-registration` role grants exactly the reads
and the provider/application writes that registration needs — no users, groups,
roles, outposts, and no deletes.

The gateway's own state is stored the same way, one level down, because it is the
one thing here that cannot be regenerated from code — its provider connections and
the key that decrypts them (`make gateway-vault-backup`, and
`make gateway-vault-check` to catch drift):

    cerulean/olympus/omniroute    SERVER_ENV (the key), PROVIDERS_JSON, provenance

The sub-path is deliberate rather than extra keys in the secret above: a KV v2
write replaces the whole secret, so folding an unrelated credential into
`cerulean/olympus` would make every re-run a quiet coin flip between the two.

Two things about that backup are traps rather than details, and both cost a
working gateway before they cost anything visible.

**The data volume has to be mounted where the image reads it.** OmniRoute's image
sets `DATA_DIR=/app/data`; a compose file that mounts the volume at `/data`
leaves the gateway keeping its connections in the container's own writable layer.
Nothing looks wrong — it answers, and its free providers keep working — until the
next `up --force-recreate`, which discards the credentials and turns every
`auto/coding` call into `503 No credentials for opencode`. The backup script no
longer guesses that path: it asks the running container for `DATA_DIR`, resolves
it through the container's mounts, and refuses with that explanation when the
data dir is not mounted at all, because in that state there is nothing durable to
back up and a volume sitting beside it is a decoy. If you change the service's
mount, run `make gateway-vault-check` — it compares against the live gateway, so
it fails on a mount that moved rather than reporting the stale copy as fine.

**The CLI has to be findable from a timer's `PATH`.** The export half of the
backup shells out to `omniroute auth export`, and `omniroute` usually lives in a
user install (`~/.nvm/versions/node/*/bin`, `~/.volta/bin`, `~/.local/bin`) that
an interactive shell has on `PATH` and a systemd unit does not. The script now
falls back to those locations and honours `OMNIROUTE_BIN`; the failure it used to
produce was the quiet kind, where running the backup by hand succeeded and the
nightly one failed, so "the backup is current" was only ever true just after
someone ran it. A backup that finds *no* connections is refused rather than
stored: an empty export is how a gateway on the wrong data dir looks, and writing
it over the good copy destroys the credentials `--restore` needs. Pass `--force`
if the emptiness is genuinely the truth.

```bash
make studio-token-check                          # remaining days; exit 2 once lapsing
make studio-token-rotate AUTHENTIK_HOST=<host>   # rebuild and re-date it
```

On the deployment host the check runs daily without anyone remembering it:

```bash
scripts/install-token-check-timer.sh             # all six: 06:17 UTC + after boot
systemctl start olympus-studio-token-check.service   # run the credential check now
systemctl start olympus-telegram-bot-check.service   # is the bot token still live now
systemctl start olympus-vault-renew-check.service    # renew the Vault token now
systemctl start olympus-build-model-check.service    # can builds produce anything now
systemctl start olympus-gateway-backup-check.service # back the gateway's state up now
```

The third is the one that guards against silence rather than an outage: builds fail
without an error when the gateway has no capacity to serve them, so
`scripts/install-token-check-timer.sh TARGET=build-model` runs
`scripts/build-model-check.py` daily and alerts when no model in the configured chain
can call a tool. See [build-model.md](build-model.md).

The fourth is the one that guards against a loss that would otherwise have no
alarm at all. It re-reads the running gateway every day and stores what it found,
which is why it needs no drift check to go with it — the copy cannot fall behind
the thing it is copied from. It is also the only target here that writes anything,
and it writes to Vault over the network, so the units' read-only hardening is
intact.

One target guards a credential that no single file owns. `TELEGRAM_BOT_TOKEN` is
shared by this stack's alert wrappers and the interactive Telegram front end, and
it can be revoked in @BotFather without anything here changing — at which point
alerts simply stop arriving. `scripts/telegram-bot-alert.sh`
(`TARGET=telegram-bot`) calls `getMe` daily and fails the unit when the token is
rejected, naming the bot when it is not. It never polls `getUpdates`, so the
front end keeps the bot's one poller. Because a revoked token is also the
credential the alert would send with, a 401 is journal-and-`systemctl --failed`
only by design.

It alerts through Telegram when the credential is inside its warning window
(`--warn-days`, 14 by default) or unusable, and repeats daily while it lapses —
a credential gating registration should nag, not hope one message lands. Set
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env` (placeholder values are
ignored) and prove the channel with `scripts/studio-token-alert.sh
--test-telegram`; until then the journal and `systemctl --failed` are the
signal. The service runs hardened and read-only, and delivery failure never
changes the verdict — the exit code is always the check's. Both wrappers share
`scripts/notify-telegram.sh`, which sends only — it never polls `getUpdates`, so
an alert can share the bot with the factory's Telegram interface without the two
fighting over updates.

It **expires** (180 days by default, `ARGS="--ttl-days 90"` to change that), so a
leaked copy stops working on its own, and `make studio-oidc-check` reports the
credential alongside the discovery probe. `make vault-bootstrap` writes the same
path from `AUTHENTIK_*` in the environment, which is how a fresh checkout seeds a
value before any credential exists; a `vault://` value there is left alone rather
than copied, so rotation owns the secret once there is one to rotate.

Rotation runs a program in the `cerulean-authentik` container's shell (`docker
exec -i cerulean-authentik ak shell`) on the host you name, and pipes its output
into a second step that writes Vault and then exercises the result: it will not
report success unless the new credential can read OAuth2 providers, *cannot* read
users, and still belongs to the service account.

It needs to reach that host as root without a password, so give the operator
account a key there once (this repo stores no key of its own; `AUTHENTIK_SSH_KEY`
points at one if it is not the default identity):

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519   # if you have not already
ssh-copy-id root@<host>                            # or append to authorized_keys there
ssh -o BatchMode=yes root@<host> true              # proves it needs no prompt
```

Two behaviours of Authentik 2026.8 are why that credential is minted in the
shell instead of over the REST API, and both were read out of the running
instance rather than assumed:

- `TokenSerializer.validate()` ends with `attrs["expires"] =
  default_token_duration()` for api-intent tokens, so a REST-created token gets
  the tenant's `default_token_duration` whatever you request — `minutes=30` on
  Cerulean. A credential that dies every half hour is not usable, so asking for
  a longer one is not a shortcut that merely fails; it silently becomes a
  useless token that still looks fine when you print it.
- `validate()` also runs `attrs.setdefault("user", request.user)`, while DRF only
  calls `validate_user` for fields *present in the payload*. A `PATCH` that never
  mentions `user` therefore re-parents the token to whoever authenticated the
  request: setting an expiry on this stack's credential through the API turned a
  service-account token into an administrator one. Nothing here PATCHes a token,
  and `--store-stdin` refuses a credential whose owner is not the service
  account.

For a checkout with no platform Vault, `compose.vault.yml` provides a dev-mode
one — in-memory and auto-unsealed, which is fine for local iteration and **not**
for anything whose secrets must survive a restart.

Never use the root token: it sits in `./data/vault/init/init.json` (0600) beside
the unseal key, and the path-scoped `olympus` token above is enough for
everything here. Re-minting it needs that root token, which stays on the Vault
host:

    vault token create -orphan -policy=olympus -period=768h

The `olympus` policy grants `read`/`list` on this path's metadata and nothing
more, so the scoped token can soft-delete a secret but cannot destroy the version
behind it — a deleted value stays readable to anyone holding the mount-wide
`cerulean` policy. Emptying a path is therefore root work on the Vault host:

```bash
vault kv destroy -versions=<n> cerulean/olympus/<path>   # burn the value(s)
vault kv metadata delete cerulean/olympus/<path>         # then the path itself
```

## Golden rules

- **Authentik = Identity** · **Cerulean Vault = Secrets** · **Cerulean = Trust** · **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Olympus · FactoryOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*


### Nightly disk cleanup

`scripts/docker-cleanup.sh` (mirrored from ips, canonical there) runs nightly at
04:17 via `/etc/cron.d/docker-cleanup`: build cache (2 GB kept), dangling and
unreferenced images, containers exited for more than a day, and container logs
over 50 MB (trimmed to 10 MB). Volumes are never touched. Run it manually with
`DRY_RUN=1 scripts/docker-cleanup.sh` to preview.
