<div align="center">

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-brightgreen.svg)](LICENSE)

# 🏭 Olympus — The AI Software Factory

**The repository-local automation platform that turns GitHub issues into validated pull requests, and a prompt into a running app — observable, gated, and self-hosted.**

Olympus is a deterministic issue → PR factory for the repo it lives in: Archon workflows (YAML DAGs) drive triage → plan → implement → independent validation → controlled merge, with protected-path enforcement, required markers, non-zero evidence counts, and stop controls. One clone, one command, and a coding agent is wired to your OmniRoute gateway — ready to run factory workflows without re-implementing identity, secrets, billing, or storage. **Studio** adds the other direction: describe an app in the browser and watch it build — plan first, then a real container running the result — and **export it to the factory** as a `build-requests/` spec, so a build you liked becomes factory input instead of stopping at the preview. **`/admin`** puts the deployment's own state on one read-only page.

[![CI](https://github.com/innotelinc/olympus/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/ci.yml)
[![Conformity](https://github.com/innotelinc/olympus/actions/workflows/conform.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/conform.yml)
[![Pages](https://github.com/innotelinc/olympus/actions/workflows/pages.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/pages.yml)

</div>

---

## What's new in this version

| Area | Change |
| --- | --- |
| **`/admin`** | The deployment panel. Every failure this deployment ever had already had a script that diagnosed it — `factory/doctor.py`, `gateway-edge-check.py`, `build-model-check.py`, `build-runner.py --list` — and not one was reachable from a browser, so the person who noticed the symptom was never the person who could run the script. `/admin` is that as a read-only page: the checks with the fix named, the gateway door and its latency, the runner heartbeat and queue, recent jobs, built apps, and the access/tenancy posture. **The door check is the one that earns it** — an `OMNIROUTE_BASE_URL` pointing at the gateway's own `:20128` on loopback compiles, resolves, and inside the container is Studio itself; the panel says that out loud and points at the SSO proxy on `:20129` instead. Gated by `OLYMPUS_ADMIN_GROUPS`, polled every 20s, and it writes nothing. |
| **Plan before build** | Studio no longer assumes a stack. A **planning turn** (`/api/plan` → `scripts/project_plan.py`) decides the language, the `install`/`build`/`start` commands, the port the app listens on, the files it will contain, and its **data target** — its own container's database, or the self-hosted Convex Atlas runs. The plan is shown for confirmation before a line of code is written, and three things then have to agree on it: the agent builds to it, `package-project.py` writes the Dockerfile from it, and `app-runtime.py` runs the container on it. Change the prompt behind a plan and it is shown as out of date rather than silently confirmed. |
| **Preview It** | Package **what is on screen**, run it, and frame it on `<slug>-preview.<suffix>` — a real runtime under a name of its own, covered by the same wildcard certificate. The project's own name is never registered by a preview, which is the whole difference from **Publish It**. A website with no plan is refused with *publish it to see it*, because static files are not a process. |
| **One gateway door** | `OMNIROUTE_BASE_URL` is `http://192.168.1.46:20129/v1` — the Authentik SSO proxy in front of the platform's one OmniRoute. `make gateway-auth-mode` leaves the gateway with no gate of its own, so its `:20128` is published on that host's loopback and bridge alone; `/v1` is refused by edge rule on the public name, because this gateway does not validate the API key and the public name would otherwise hand out unauthenticated inference. |
| **Tenancy** | With `CONTROL_PLANE_INTERNAL_URL` + `CONTROL_INTERNAL_TOKEN` set, every Studio turn is spent on the signed-in user's **own** gateway key: identity resolved, quota gated before dispatch, usage recorded after, and audit rows for build/publish/export. The library follows the control-plane account, and an existing subject-keyed library is adopted in place. |
| **Build runner** | The account is **probed, not assumed** — a build runs a coding agent inside Codex's bubblewrap sandbox, and bubblewrap needs unprivileged user namespaces. Where they work the installer uses a dedicated `olympus-builder` account; where they are denied (including container setups) it stays root, because a non-root runner there means an *unsandboxed* agent rather than a safer one. Either way the agent stays sandboxed, and a cancel signals the whole process group. |
| **BuildKit** | `scripts/buildx` is the one command every packager runs — `docker build` when the daemon has no buildx, a named builder when `BUILDX_NAME` is set, and `--check` prints which builder a host will actually use instead of leaving it to be inferred later. |
| **SecretOps** | Infisical is **replaced by Cerulean Vault** (KV v2); `infisical://` becomes `vault://`, and the bootstrap helper is `scripts/omniroute-vault.sh`. |
| **Security** | `scripts/secret-scan.py` fails the build on secret-shaped files; the Studio session and Vault token are scoped per stack. |

Full history and what is open: **[docs/roadmap.md](docs/roadmap.md)** — also on the [landing page](https://innotelinc.github.io/olympus/#roadmap).

---

## Why Olympus

| Problem | Olympus answer |
| --- | --- |
| Issues stall between triage and a trustworthy PR | Deterministic factory workflows (prime → implement → validate, plus regress and triage) with independent validation |
| Automation that silently skips checks or walks past missing evidence | Non-zero evidence counts, required markers, ratchet floors, and fail-closed behavior are enforced |
| Agents that touch governance or secrets as ordinary work | Protected paths, secret-shaped-file blocks, and bounded PR size — fix the source, never the harness |
| Heavy model inference that heats the local CPU | 100% of heavy inference routed to free cloud models via the OmniRoute gateway (Codex `auto/coding` + Hermes 3 via OpenRouter), local Ollama bypassed |
| No visibility into autonomy, blockers, or held work | `factory/doctor.py`, `factory/trigger.py --status`, and the Studio **`/admin`** panel report readiness, autonomy, evidence, and what is held |
| Building a small app means leaving the platform for a chat window | [Studio](web/studio/) puts plan → prompt → files → running preview in the browser, behind the same gateway, OIDC and secret rules |
| "It's not working" arriving as a screenshot | The failure scripts are a page now, each check naming its own fix |

> **About Olympus** — a repository-local AI software factory built with three upstream frameworks vendored under `core-modules/` — [OmniRoute](https://github.com/innotelinc/omniroute) (cloud routing gateway), [Archon](https://github.com/innotelinc/Archon) (YAML DAG workflow engine), and the [AI Software Factory](https://github.com/innotelinc/ai-software-factory) (SDLC scheduling and task consumer). Those point at the `innotelinc` mirrors on purpose: the upstream URLs this project originally referenced (`inotex/omniroute`, `JohanLi233/archon`, `Andy-Zhouelect/AI-Software-Factory`) are **404** — see [UPSTREAMS.md](UPSTREAMS.md) for provenance, pinned SHAs, licenses and how to re-sync a mirror. The local CPU stays cool because the coding brain (**Codex** via OmniRoute `auto/coding`, Responses API) and the Telegram frontend (**Hermes 3 70B** via OpenRouter Free) never load weights locally — they run behind the gateway (Telegram at `nousresearch/hermes-3-llama-3-70b:free`). **Landing page:** [innotelinc.github.io/olympus](https://innotelinc.github.io/olympus)

---

## What it is

- **Owns:** repository automation (issue → watched workflow → open PR), validation and safety (protected paths, required markers, evidence counts, stop controls), and operational visibility (`factory/doctor.py`, `factory/trigger.py`, Studio `/admin`)
- **Owns:** the five Archon factory workflows and the harness as the definition of "working" — the harness is never edited to make a check pass
- **Provides:** a one-command agent-ready clone (`bash scripts/bootstrap.sh`) and an interactive Telegram surface for issue → fix laps
- **Provides:** [Studio](web/studio/) — the browser build surface: plan first, then describe an app in plain language, watch it build, run and publish it, all against the same OmniRoute gateway (`make studio-dev`)
- **Provides:** the installer, the compose stacks, the landing page and this documentation — this repository is where an operator gets a working factory, not where its governance lives (see [What this repo does and does not contain](#what-this-repo-does-and-does-not-contain))
- **Fast lane:** Issue → Fix PR in ~35 minutes including autonomous code review
- **Classification:** **FactoryOps** — see [docs/stack.md](docs/stack.md)

---

## Quick start

Three entry points. Pick one — they are independent, and `make help` lists every target.

### 1. Local (clone → ready)

```bash
git clone https://github.com/innotelinc/olympus.git
cd olympus
bash scripts/bootstrap.sh          # idempotent
```

### 2. Docker (one command)

```bash
git clone https://github.com/innotelinc/olympus.git
cd olympus
make setup                         # preflight + .env with generated secrets
$EDITOR .env                       # OMNIROUTE_* / OIDC_* / VAULT_* — see .env.example
make docker-up                     # builds ghcr.io/innotelinc/olympus:local + starts
make docker-logs                   # tail factory + gateway logs
```

### 3. Studio (the web UI)

```bash
make studio-install                # install web/studio dependencies (npm ci)
make studio-dev                    # dev server → http://localhost:3001
make studio-test                   # vitest: parser, plan, gateway route, OIDC flow, admin
make studio-export-dir             # let Studio write build-requests/ specs (uid 1001)
make studio-build-queue-dir        # let Studio queue builds (uid 1001)

# "Build it": Studio's build runs on the host, not in its own container.
# Studio's image is a traced Next.js bundle — no Archon CLI, no Codex CLI, no
# checkout — so it queues a request and this service, which runs where the
# toolchain is, executes the same command `make app` does and records the result.
sudo make build-runner-install     # systemd service (idempotent; --uninstall to remove)
make build-runner-check            # what the runner will use: gateway, model, tools
make build-runner-list             # the queue, and where each build got to
make build-model-check             # can the configured model call a tool, twice over?
make test-runner                   # the runner's unit tests
make prune                         # old builds + finished queue files (ARGS="--yes" to delete)

# ...or inside the stack
docker compose up -d studio

# ...but when the gateway is published on loopback on this host, Studio has to
# share the host's network namespace. This target cannot get that wrong:
make docker-studio-up
```

### 4. The deployment panel — `/admin`

Every failure this deployment has had already had a script that diagnosed it; the
panel is those scripts on one read-only page, so the person who sees the symptom can
see the cause.

```
https://<studio-host>/admin
```

It reads the gateway door and its latency, the runner heartbeat and queue, recent
jobs, built apps on disk, and the access/tenancy posture — plus a check list that
names the fix. It writes nothing, and it is gated by `OLYMPUS_ADMIN_GROUPS`
(comma-separated, exact match, re-checked per request; empty = any Studio user,
which is the right default for a single-operator deployment and is reported on the
page). See [web/studio/README.md](web/studio/README.md#the-deployment-panel-admin).

### 5. The terminal UI — the same builder, in a shell

The builder has two front ends and only one of them had a UI. This is the other: one
window that queues a build and streams the status, the steps and the log that were
previously spread across `make app`, `make build-runner-list` and `tail`.

```bash
make tui                                                  # interactive
make tui ARGS="--list"                                    # queued, running, finished
make tui ARGS="--once 'a weight tracker with a weekly chart'"
```

It is a front end to the *same* builder, not a second one. The instruction becomes a
spec in `build-requests/`, and the runner builds it down the identical path `make app`
and the browser's Build It use — this adds a view, and no second implementation of a
plan or a generation prompt. Inside it, `/preview <slug>` and `/publish <slug>` queue a
delivery for an app already in `builds/<slug>/`, carrying that app's source the way the
browser does, and follow it.

### 6. Delivering a build: a name, a container, or a zip

Studio's two kinds leave by different doors, and the difference is whether the
thing has state.

```bash
# A WEBSITE is static files: staged here, served by olympus-sites, named at the edge.
make sites-up                                  # the server both kinds are fronted by
make sites-wildcard                            # once: *.studio.olympus.innotel.us + one certificate
make site-publish SLUG=todo-list               # package, stage, and put it on a name
make site-check HOST=todo-list.studio.olympus.innotel.us

# An APP is a running service: React client + its own API + SQLite.
make app-package SLUG=weight-tracker           # build the client, write the archive
make app-up SLUG=weight-tracker                # image, container, loopback port, nginx vhost
make app-publish SLUG=weight-tracker           # the three above, then the name
make apps-list
make app-down SLUG=weight-tracker              # stop it, keep the database
```

Both are published through Cerulean + NPM under the same wildcard, and
`studio-sites.py` does not know which is which — the app's generated vhost does the
routing, so the edge never learns a per-app port. Each app's data lives on the host
at `$OLYMPUS_APPS_ROOT/data/<slug>/`, so a rebuild keeps it. See
[docs/site-publishing.md](docs/site-publishing.md).

Studio needs a gateway and (optionally) OIDC. `make setup` scaffolds `.env`; the keys that matter are `OMNIROUTE_BASE_URL` + `OMNIROUTE_API_KEY` for generation, and `OIDC_ISSUER_URL` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_REDIRECT_URI` / `STUDIO_SESSION_SECRET` for sign-in. **Auth stays off until it is configured** — run `make studio-oidc` (or `python3 scripts/authentik-studio-app.py`) to create or repair the Cerulean Authentik application and provider for it. See [web/studio/README.md](web/studio/README.md) for the full contract.

Generated apps are saved per signed-in identity, so a reload or a rebuild no longer loses them. With `CONTROL_PLANE_INTERNAL_URL` + `CONTROL_INTERNAL_TOKEN` set, the library follows the control-plane user id and each turn is spent on that user's **own** gateway key, with a quota check before it and usage recorded after it, rather than on the one shared `OMNIROUTE_API_KEY`. An existing subject-keyed library is adopted in place. See [web/studio/README.md](web/studio/README.md#tenancy--whose-key-pays).

Then manufacture an app from a spec:

```bash
make new-request NAME=my-todo   # → build-requests/my-todo.md
$EDITOR build-requests/my-todo.md
make app                         # local — or: make docker-app SPEC=build-requests/my-todo.md
make builds                       # list ./builds (volume, gitignored)
```

`make app` runs the `archon-greenfield` workflow (`.archon/workflows/app/greenfield/`)
through the Archon CLI: it resolves and bounds the spec, **plans the stack**, runs Codex
over the plan, **asserts on the artifact rather than the exit code**, and only then
writes a `MANIFEST.json` + `README.md` recording which spec (by SHA-256) and which model
produced the app. An agent that exits 0 having written nothing fails the run — worth
knowing because this gateway does that: Codex exits 0 even when its last request was
refused.

**The stack is planned before the agent runs.** One turn against the gateway decides the
language, the commands that install, build and start the project, the port it listens on
and the files it will contain, and writes `plan.json` into the app directory. Three
things read that file and must agree on it: the agent is told the plan and builds to it,
`scripts/package-project.py` writes the Dockerfile from it, and `scripts/app-runtime.py`
runs the container on its port. The plan also names the project's **data target**: its
own container's database by default, or the self-hosted Convex that Atlas runs when a
request genuinely needs live, shared state. A planning turn that produces no usable plan
stops the run. By hand: `python3 scripts/project_plan.py --spec build-requests/my-todo.md`.

The build node retries while the app directory is still empty, and switches model
between attempts: `OMNIROUTE_MODEL` first, then `OMNIROUTE_MODEL_FALLBACK`, then
`auto/coding` as a **last** attempt. Changing `.env` is how you change the build
model; exporting the variable does not survive, because Archon strips the repo's own
`.env` keys from a script node's environment *and* runs the workflow from a copy under
`artifacts/runs/<id>/`.

Pin a **concrete, tool-calling model**. A model that answers in prose still "completes"
while writing nothing, which shows up as a build that runs for minutes and leaves an
empty directory with no error to read:

* `auto/coding` is a **combo** that walks the whole gateway catalogue — `Trying model
  1109/1388` before it answered — so which brain finishes a build is luck.
* A connection count is not quota: of ten connections recovered here, agentrouter was
  `402 budget pool exhausted`, openrouter **out of credits** and openai `429 no credits`,
  and the gemini free tier cools down for 15–23 minutes after a burst.

`make build-model-check` does the check for the whole configured chain — two turns,
since the failure that cost the most here was on the second one — and exits `1` when a
build would exit 0 having written nothing. `scripts/install-token-check-timer.sh
TARGET=build-model` alerts on it daily, and `TARGET=gateway-backup` keeps the gateway's
provider connections and the key that decrypts them in Vault on the same schedule.
**The capacity story and the options for fixing it are in
[docs/build-model.md](docs/build-model.md)** — worth reading before adding credentials.

Docker also runs the same `push` manufacture trigger in CI: `.github/workflows/olympus-app-builder.yml`
(`on.push.paths: build-requests/*.md`).

The bootstrap is idempotent and does, in order:

1. Installs what is missing: the OmniRoute CLI, the Codex CLI, the Claude Code CLI, and the Archon CLI.
2. Checks that an OmniRoute server is reachable. If not, tries `scripts/omniroute-vault.sh` (Vault-backed); otherwise prints how to start it and continues.
3. Creates an OmniRoute API key and stores it in `~/.omniroute/.env` (never in the repo).
4. Wires both agents to OmniRoute: Codex → `~/.codex/config.toml` + `~/.codex/auth.json`, Claude Code → `~/.claude/settings.json`.
5. Runs `python3 factory/doctor.py` so you can see readiness at a glance.

Then run the factory manually at autonomy level 0 (nothing unattended until a human has watched a real lap):

```bash
# Local
python3 factory/doctor.py
archon workflow run factory-implement --branch factory/impl-<n> "implement gh:issue:<n>"
archon workflow run factory-validate --branch factory/val-<n> "validate gh:pr:<n>"

# Docker
make docker-shell
python3 factory/doctor.py
# or from the host: docker compose exec olympus bash scripts/manufacture.sh build-requests/my-todo.md
```

### Installation details

| Step | Command / file | Notes |
| --- | --- | --- |
| Prerequisites | `make setup` | Preflight (git, curl, python3, node, docker where needed), installs the attribution guard hooks, generates `.env` with fresh secrets |
| Environment | `.env.example` → `.env` | `make setup` never overwrites an existing `.env`; new keys are appended on upgrade. `make env-sync` reports any key the example documents that `.env` has never mentioned (exit 2), `make env-sync-write` appends them with the example's own comments. Neither edits, reorders or removes a line already there, and a key present but commented out counts as present |
| Gateway | `OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY` | **The gateway is a shared platform service and this stack starts none** — the single OmniRoute runs in Group 2 (`2-voice/`), on the Cerulean/trust host `192.168.1.46`, so one provider pool serves every platform. The door is the Authentik SSO proxy in front of it, `:20129`: `make gateway-auth-mode` leaves the gateway with no gate of its own, so its own `:20128` is published on that host's loopback and bridge alone and is never dialled across a network. Default `OMNIROUTE_BASE_URL=http://192.168.1.46:20129/v1`; on the gateway's own host a container uses `http://host.docker.internal:20129/v1` and a host-mode caller `http://127.0.0.1:20129/v1` (with `compose.host-gateway.yml` — `make docker-studio-up`). Not the published name: `/v1` is refused there by edge rule (`make gateway-edge --deny-path /v1`), because this gateway does not validate the API key and the public name would otherwise hand out unauthenticated inference. See [docs/gateway-sso.md](docs/gateway-sso.md) |
| SecretOps | `compose.vault.yml` + `scripts/vault-bootstrap.py` | Cerulean Vault (KV v2). `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_PREFIX`; values may be `vault://<name>` references resolved at startup. Tokens are scoped to this stack's own path |
| Identity | `scripts/authentik-studio-app.py` | Creates the Studio application/provider in Cerulean Authentik; Studio is public until OIDC is set. The gateway dashboard's client is registered the same way |
| Gateway dashboard | `make gateway-oidc`, `gateway-sso-up`, `gateway-auth-mode`, `gateway-edge`, `gateway-edge-check` | Puts the OmniRoute dashboard behind Authentik via an identity-aware proxy, restricted to a group, and publishes it — DNS record, certificate and NPM host — through Cerulean. `gateway-auth-mode` turns the gateway's *own* login off so Authentik is the only gate; because that makes the port's reachability the whole control, it checks the binding first (loopback plus this host's own docker0, never a LAN address) and refuses otherwise. The check walks the public name link by link and names the first one that is broken. See [docs/gateway-sso.md](docs/gateway-sso.md) |
| Gateway state | `make gateway-vault-backup`, `gateway-vault-check`, `gateway-vault-restore` | The gateway's provider connections *and* the key that decrypts them, into Cerulean Vault. `--check` exits non-zero when the backup has drifted from the live gateway, so it can go on a timer |
| Trust / edge | `scripts/` (NPM + Cerulean) | DNS and TLS for the public names are managed through Cerulean and NPM Edge; see [docs/stack.md](docs/stack.md) |
| Upgrade | `git pull && make setup` | Idempotent; `scripts/factory-pin.sh` pins the factory's upstream SHAs. `make setup` ends with `env-sync --write`, so a release that adds a knob shows up in `.env` instead of running on a built-in default nobody can see |
| Verify | `make doctor`, `make studio-test`, `make ps` | Readiness, Studio test suite, supporting-service status |

> **Not published in this repo.** `MISSION.md`, `FACTORY.md`, `FACTORY_RULES.md`, `AGENTS.md`, `harness/`,
> `factory/*` (beyond `APP_SPEC_TEMPLATE.md`, `doctor.py` and `trigger.py`), `.archon/workflows/factory/` and
> `.factory/` state are deliberately gitignored — see [What this repo does and does not contain](#what-this-repo-does-and-does-not-contain).
> An agent-ready clone installs them; a plain clone of this repository has the installer without the governance.

---

## What this repo does and does not contain

This repository is the **installer and operator surface** for Olympus. `.gitignore` is the
contract, not an accident:

- **Published:** `scripts/` (bootstrap, manufacture, Vault, secret scan, factory pin, `buildx`), `factory/doctor.py`,
  `factory/trigger.py`, `factory/APP_SPEC_TEMPLATE.md`, `web/studio/`, `web/landing/`, `docs/`,
  `docker-compose.yml`, `compose.vault.yml`, `compose.host-gateway.yml`, `compose.gateway-sso.yml`, `Dockerfile`, `Makefile`, `UPSTREAMS.md`
- **Not published (gitignored):** `MISSION.md`, `FACTORY.md`, `FACTORY_RULES.md`, `harness/`,
  `.factory/` runtime state, `.archon/workflows/factory/`, and `core-modules/*` — the vendored upstreams are
  cloned by `setup.sh` / `bootstrap.sh` on first run, so provenance stays pinned in [UPSTREAMS.md](UPSTREAMS.md)
  instead of drifting with a copy in git

If you are looking for the safety rules, the holdout contract, or the five YAML DAGs, they live in the
source checkout of the factory — not in this distribution repo.

---

## Roadmap

Autonomy is earned. The full, current roadmap is [**docs/roadmap.md**](docs/roadmap.md) —
which is also rendered at [innotelinc.github.io/olympus/#roadmap](https://innotelinc.github.io/olympus/#roadmap).

| Milestone | What it is | State |
| --- | --- | --- |
| **0.1 — the builder** | Studio, the TUI and the plan → stream → package → run → publish engine, with the build runner and the saved-app store | **shipped** |
| **0.2 — the operator surface** | `/admin`, plan-before-build, Preview It, one gateway door, tenancy, runner hardening | **in development** |
| **0.3 — dispatch** | Arm the dispatcher after a watched lap; publish-time observability | next |
| **later** | Autonomy L2 (validate) and L3 (holdout + mutation ratchet); ONYX storage, on hold | later |

---

## Documentation

| Document | What it covers |
| --- | --- |
| [docs/roadmap.md](docs/roadmap.md) | The current roadmap — milestones, the open 0.2 items, and the L0–L3 autonomy ladder |
| [web/studio/README.md](web/studio/README.md) | Studio — the build UI: the plan, the six deliveries, tenancy, the `/admin` panel, configuration, output contract, security posture |
| `scripts/olympus-tui.py` | The builder's terminal front end (`make tui`) — what it reads, the keys, and why it queues rather than planning or generating |
| [docs/stack.md](docs/stack.md) | This platform's role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) (FactoryOps), including SecretOps via Cerulean Vault (KV v2) and the `vault://` reference convention |
| [docs/gateway-sso.md](docs/gateway-sso.md) | Putting the OmniRoute dashboard behind Cerulean Authentik, `make gateway-auth-mode`, and why the gateway cannot gate itself |
| [docs/site-publishing.md](docs/site-publishing.md) | Studio's two kinds of build — an **app** (React + API + SQLite, one container per app) and a **website** (static) — the packaging/runtime/publish pipelines, previews, the plan's Convex target, and the ONYX (on hold) integration points |
| [docs/host-migration.md](docs/host-migration.md) | Consolidating Olympus onto `192.168.1.46` — the two-host split that made every sign-in fail with `invalid_client` while the local `.env` was correct |
| [docs/build-model.md](docs/build-model.md) | The capacity story behind the build model: which connections were measured as unusable, what `make build-model-check` proves and does not, and the durable fix |
| `scripts/verify-sso.py` | The Studio sign-in test — client credentials at the token endpoint, the edge's forward target, anonymous refusal, and a full authorization-code flow |
| [UPSTREAMS.md](UPSTREAMS.md) | Vendored upstream mirrors (Archon, AI Software Factory, skills, OmniRoute, archon-cli) — provenance, pinned SHAs, licenses, recovery and re-sync |
| [factory/APP_SPEC_TEMPLATE.md](factory/APP_SPEC_TEMPLATE.md) | The spec format `make new-request` scaffolds and the manufacture step reads |
| `.env.example` | Every knob the stack reads, grouped and commented (gateway, Studio, OIDC, Vault, Cerulean, NPM, Magnate) |
| `Makefile` | The full target list — `make help` |
| `MISSION.md`, `FACTORY.md`, `FACTORY_RULES.md`, `AGENTS.md`, `harness/END-TO-END.md`, `.factory/holdout/HOLDOUT.md` | Scope, operations, safety rules and the holdout contract — **in the source checkout**, gitignored here |

---

## License

Olympus is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text. Upstream frameworks under `core-modules/` retain their own licenses in-tree.

---

*Olympus · FactoryOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) — one job per platform, platform services consumed by business functions.*
