<div align="center">

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-brightgreen.svg)](LICENSE)

# 🏭 Olympus — The AI Software Factory

**The repository-local automation platform that turns GitHub issues into validated pull requests — observable, gated, and self-hosted.**

Olympus is a deterministic issue → PR factory for the repo it lives in: Archon workflows (YAML DAGs) drive triage → plan → implement → independent validation → controlled merge, with protected-path enforcement, required markers, non-zero evidence counts, and stop controls. One clone, one command, and a coding agent is wired to your OmniRoute gateway — ready to run factory workflows without re-implementing identity, secrets, billing, or storage. **Studio** adds the other direction: describe an app in the browser and watch it build against the same gateway — then **export it to the factory** as a `build-requests/` spec, so a build you liked becomes factory input instead of stopping at the preview.

[![CI](https://github.com/innotelinc/olympus/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/ci.yml)
[![Conformity](https://github.com/innotelinc/olympus/actions/workflows/conform.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/conform.yml)
[![Pages](https://github.com/innotelinc/olympus/actions/workflows/pages.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/pages.yml)

</div>

---

## What's new in this version

| Area | Change |
| --- | --- |
| **Studio** | `web/studio/` — the browser vibe-coding surface. Prompt → streamed files, Authentik OIDC sign-in, sandboxed output, saved builds scoped to the signed-in identity, and six deliveries with one job each: **Build It** (write or add on), **Factory Build** (`make app` for real on the host runner, with its progress and a **Cancel**), **Preview It** (package *what is on screen*, run it and frame it on `<slug>-preview.<suffix>` — a real runtime, no name registered), **Publish It** (put *what is on screen* on its own name), **Export It** (a `build-requests/` spec for CI or a hand-off), **Download It** (a zip). Two kinds: an **app** keeps state on a server and runs as one container per app; a **website** is static content served over HTTP. The stack is not fixed — a plan is proposed from your prompt, shown for confirmation, and then built, packaged and run from it (`scripts/package-project.py`, `scripts/app-runtime.py`). Runs on its own port, in the stack (`docker compose up -d studio`) or standalone (`make studio-dev`). |
| **SecretOps** | Infisical is **replaced by Cerulean Vault** (KV v2). The `infisical://` reference convention becomes `vault://`, and the bootstrap helper is now `scripts/omniroute-vault.sh` (was `scripts/omniroute-infisical.sh`). |
| **Compose** | `compose.vault.yml` runs Vault locally; `compose.host-gateway.yml` is a host-network override for when the gateway is published on loopback only. |
| **Factory** | `factory/doctor.py` and `factory/trigger.py` are published and report the factory's **real** state (no simulated poll loop); CI pins the Archon integration and only manufactures a real new spec. |
| **Security** | `scripts/secret-scan.py` fails the build on secret-shaped files; the Studio session and Vault token are scoped per stack. |

---

## Why Olympus

| Problem | Olympus answer |
| --- | --- |
| Issues stall between triage and a trustworthy PR | Deterministic factory workflows (prime → implement → validate, plus regress and triage) with independent validation |
| Automation that silently skips checks or walks past missing evidence | Non-zero evidence counts, required markers, ratchet floors, and fail-closed behavior are enforced |
| Agents that touch governance or secrets as ordinary work | Protected paths, secret-shaped-file blocks, and bounded PR size — fix the source, never the harness |
| Heavy model inference that heats the local CPU | 100% of heavy inference routed to free cloud models via the OmniRoute gateway (Codex `auto/coding` + Hermes 3 via OpenRouter), local Ollama bypassed |
| No visibility into autonomy, blockers, or held work | `factory/doctor.py` and `factory/trigger.py --status` report readiness, autonomy, evidence, and what is held |
| Building a small app means leaving the platform for a chat window | [Studio](web/studio/) puts prompt → files → running preview in the browser, behind the same gateway, OIDC and secret rules |

> **About Olympus** — a repository-local AI software factory built with three upstream frameworks vendored under `core-modules/` — [OmniRoute](https://github.com/innotelinc/omniroute) (cloud routing gateway), [Archon](https://github.com/innotelinc/Archon) (YAML DAG workflow engine), and the [AI Software Factory](https://github.com/innotelinc/ai-software-factory) (SDLC scheduling and task consumer). Those point at the `innotelinc` mirrors on purpose: the upstream URLs this project originally referenced (`inotex/omniroute`, `JohanLi233/archon`, `Andy-Zhouelect/AI-Software-Factory`) are **404** — see [UPSTREAMS.md](UPSTREAMS.md) for provenance, pinned SHAs, licenses and how to re-sync a mirror. The local CPU stays cool because the coding brain (**Codex** via OmniRoute `auto/coding`, Responses API) and the Telegram frontend (**Hermes 3 70B** via OpenRouter Free) never load weights locally — they run behind the gateway at `http://localhost:20128/v1` (Telegram at `nousresearch/hermes-3-llama-3-70b:free`). **Landing page:** [innotelinc.github.io/olympus](https://innotelinc.github.io/olympus)

---

## What it is

- **Owns:** repository automation (issue → watched workflow → open PR), validation and safety (protected paths, required markers, evidence counts, stop controls), and operational visibility (`factory/doctor.py`, `factory/trigger.py`)
- **Owns:** the five Archon factory workflows and the harness as the definition of "working" — the harness is never edited to make a check pass
- **Provides:** a one-command agent-ready clone (`bash scripts/bootstrap.sh`) and an interactive Telegram surface for issue → fix laps
- **Provides:** [Studio](web/studio/) — the browser vibe-coding surface: describe an app in plain language, watch it build, iterate on it, all against the same OmniRoute gateway (`make studio-dev`)
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
make studio-test                   # vitest: parser, gateway route, OIDC flow, saved apps
make studio-export-dir             # let Studio write build-requests/ specs (uid 1001)
make studio-build-queue-dir        # let Studio queue builds (uid 1001)

# "Build it": Studio's build runs on the host, not in its own container.
# Studio's image is a traced Next.js bundle — no Archon CLI, no Codex CLI, no
# checkout — so it queues a request and this service, which runs where the
# toolchain is, executes the same command `make app` does and records the result.
# The account is probed, not assumed: the agent works inside Codex's bubblewrap
# sandbox, which needs unprivileged user namespaces — available on some hosts, denied
# on others (including container setups). Where they work the installer uses a
# dedicated `olympus-builder` account and hands it the queue, `builds/` and an ACL
# on `.env`; where they are denied it stays root, because a non-root runner there
# means an *unsandboxed* agent rather than a safer one. Either way the agent stays
# sandboxed. `--as-user` / `--as-root` override the probe.
sudo make build-runner-install     # systemd service (idempotent; --uninstall to remove)
make build-runner-check            # what the runner will use: gateway, model, tools
make build-runner-list             # the queue, and where each build got to
make build-model-check             # can the configured model call a tool, twice over?
make test-runner                   # the runner's unit tests
make prune                         # old builds + finished queue files (ARGS="--yes" to delete)

# The gateway's provider credentials are deployment state, not code. If the
# gateway ever comes back on a fresh data dir it has none, and `auto/coding`
# then pins to a provider that 503s on every turn after the first.
scripts/omniroute-restore-providers.py --dry-run   # what would be copied
scripts/omniroute-restore-providers.py             # copy them, and clear the pin

# ...and the reason to hold a copy of them somewhere else: the key that decrypts
# those connections lives in that same volume, so the volume IS the gateway.
make gateway-vault-backup                          # connections + keys -> Vault
make gateway-vault-check                           # non-zero if it has drifted

# or inside the stack
docker compose up -d studio

# ...but when the gateway is published on loopback on this host, Studio has to
# share the host's network namespace: its OMNIROUTE_BASE_URL is 127.0.0.1, which
# on a bridge network is Studio itself. Generation then fails ECONNREFUSED with
# nothing in the UI saying so. This target cannot get that wrong:
make docker-studio-up
```

### 4. The terminal UI — the same builder, in a shell

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
plan or a generation prompt, which is the drift `scripts/project_plan.py` exists to
prevent. It does not plan, it does not generate: it queues and it watches. Inside it,
`/preview <slug>` and `/publish <slug>` queue a delivery for an app already in
`builds/<slug>/`, carrying that app's source the way the browser does, and follow it.

### 5. Delivering a build: a name, a container, or a zip

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

Generated apps are saved per signed-in identity: the OIDC subject decides whose library a build lands in, so a reload or a rebuild no longer loses it. In the stack the library is the `studio-data` volume (`STUDIO_DATA_DIR` points at it).

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
runs the container on its port. A planning turn that produces no usable plan stops the
run, because the alternative is minutes of agent time against a stack nobody chose —
which is what used to happen: the prompt said "match the spec's tech stack" while the
packager demanded a fixed React + Node/SQLite scaffold, so a spec that asked for anything
else was built to neither and failed at packaging. By hand:
`python3 scripts/project_plan.py --spec build-requests/my-todo.md`.

The build node retries while the app directory is still empty, and switches model
between attempts: `OMNIROUTE_MODEL` first, then `OMNIROUTE_MODEL_FALLBACK`, then
`auto/coding` as a **last** attempt. Both of the first two are read
from this checkout's `.env`, and that is not a detail you can skip — Archon strips the
repo's own `.env` keys out of a script node's environment ("stripped 42 keys") *and* runs
the workflow from a copy under `artifacts/runs/<id>/`, so the node used to fall back to
its code defaults while `.env` said otherwise: a deployment pinned to
`gemini/gemini-3-flash-preview` asked the gateway for `auto/coding` and the manifest
recorded the model nobody configured. Changing `.env` is therefore how you change the
build model; exporting the variable does not survive.

The third attempt is measured rather than theoretical. This gateway's models run on free
tiers that go into cooldown, and a run whose two configured models were both unavailable —
`oc/mimo-v2.5-free` timing out upstream and `gemini/gemini-3-flash-preview` reporting
`model_cooldown` with a 23-minute reset — failed three times in a row while a third of the
catalogue was answering. The composed route walks the catalogue, so it is placed last on
purpose: ahead of it the configured model is the deliberate choice, behind it luck is
better than nothing. The **planning** node uses the same chain, in the same order, so a
spec cannot plan with one model and generate with another.

**A tree that installs and then fails to compile goes back to the agent that wrote it.**
The build node runs the plan's own install and build against what the agent wrote — the
same commands the Dockerfile will run — and a non-zero exit becomes the next instruction
to the same agent, with the compiler's output attached and the plan still in front of it,
once. Nothing used to run the plan's build: `verify-app` runs a check the *spec* declares,
and the packager runs the plan in the image, after the node is gone — so a project that
wrote itself and did not compile was reported built and failed later as a packaging error
with nobody left to act on it. A build the node cannot run is skipped rather than blamed
on the agent, since the packager installs its own toolchain in the image — and so is a
plan in a language whose install would write *outside* the project (`pip install` would
land in whatever interpreter is on PATH), because this node runs on the host. Those are
still built and repaired in their image. The outcome is recorded as `build_exit` and
`repairs` so "the agent gave up" and "the tree does not compile" stay distinguishable.

Pin a **concrete, tool-calling model**. A model that answers in prose still "completes"
while writing nothing, which shows up as a build that runs for minutes and leaves an
empty directory with no error to read:

* `auto/coding` is a **combo** that walks the whole gateway catalogue — `Trying model
  1109/1388` before it answered — so which brain finishes a build is luck. Routed to the
  free `oc/big-pickle` the agent wrote the file out as chat text and produced nothing for
  eight minutes; routed further along it built the same spec correctly in 2m18s. It also
  pins a native Codex turn to the member that served the first turn, which answers `503
  No credentials for opencode` where that member has none.
* Restoring the provider connections (`scripts/omniroute-restore-providers.py`) does not
  settle it: of the ten accounts recovered here, agentrouter was `402 budget pool
  exhausted`, openrouter **out of credits** and openai `429 no credits` — only the gemini
  accounts had live quota, and their free tier cools down for 15–23 minutes after a burst.

So: a concrete model first, a second entry for when the primary is cooling, and check any
candidate with a tool-calling request to `/v1/responses` — a 200 is not the check, a
`function_call` in the response is. The shape is in `.env.example`, along with the models
measured as unusable here.

`make build-model-check` does that check for the whole configured chain — two turns, since
the failure that cost the most here was on the second one — and exits `1` when a build
would exit 0 having written nothing. `scripts/install-token-check-timer.sh
TARGET=build-model` alerts on it daily, and `TARGET=gateway-backup` keeps the gateway's
provider connections and the key that decrypts them in Vault on the same schedule.
**The capacity story, the options for fixing it,
and what a pass does not prove are in [docs/build-model.md](docs/build-model.md)** — worth
reading before adding credentials, because a gateway's connection count is not its quota.

Docker also runs the same `push` manufacture trigger in CI: `.github/workflows/olympus-app-builder.yml`
(`on.push.paths: build-requests/*.md`).

The bootstrap is idempotent and does, in order:

1. Installs what is missing: the OmniRoute CLI, the Codex CLI, the Claude Code CLI, and the Archon CLI.
2. Checks that an OmniRoute server is reachable (`http://localhost:20128`; override with `OMNIROUTE_BASE_URL`). If not, tries `scripts/omniroute-vault.sh` (Vault-backed); otherwise prints how to start it and continues.
3. Creates an OmniRoute API key and stores it in `~/.omniroute/.env` (never in the repo).
4. Wires both agents to OmniRoute: Codex → `~/.codex/config.toml` + `~/.codex/auth.json`, Claude Code → `~/.claude/settings.json` (`ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`).
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
| Environment | `.env.example` → `.env` | `make setup` never overwrites an existing `.env`; new keys are appended on upgrade. `make env-sync` reports any key the example documents that `.env` has never mentioned (exit 2), `make env-sync-write` appends them with the example's own comments. Neither edits, reorders or removes a line already there, and a key present but commented out counts as present — `# STUDIO_DATA_DIR=…` is a decision, not a gap |
| Gateway | `OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY` | Run it here? `make gateway-up` (the `gateway` profile in `docker-compose.yml`, data in the `omniroute-data` volume). Remote gateway? point the URL at it. Loopback-published on this host? the things that talk to it — Studio, the SSO proxy — need `compose.host-gateway.yml`. **Another machine on the LAN:** `OMNIROUTE_BASE_URL=https://gateway.olympus.innotel.us/v1` (or `http://<this-host>:20129/v1`) with the same key — the dashboard behind it still requires Authentik |
| SecretOps | `compose.vault.yml` + `scripts/vault-bootstrap.py` | Cerulean Vault (KV v2). `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_PREFIX`; values may be `vault://<name>` references resolved at startup. Tokens are scoped to this stack's own path |
| Identity | `scripts/authentik-studio-app.py` | Creates the Studio application/provider in Cerulean Authentik; Studio is public until OIDC is set. The gateway dashboard's client is registered the same way |
| Gateway dashboard | `make gateway-oidc`, `gateway-sso-up`, `gateway-auth-mode`, `gateway-edge`, `gateway-edge-check` | Puts the OmniRoute dashboard behind Authentik via an identity-aware proxy, restricted to a group, and publishes it — DNS record, certificate and NPM host — through Cerulean. `gateway-auth-mode` turns the gateway's *own* login off so Authentik is the only gate; because that makes the loopback binding the whole control, it checks the binding first and refuses otherwise. The check walks the public name link by link and names the first one that is broken, because the report that arrives is "it's not resolving" and that is almost never what happened. See [docs/gateway-sso.md](docs/gateway-sso.md) |
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

- **Published:** `scripts/` (bootstrap, manufacture, Vault, secret scan, factory pin), `factory/doctor.py`,
  `factory/trigger.py`, `factory/APP_SPEC_TEMPLATE.md`, `web/studio/`, `web/landing/`, `docs/`,
  `docker-compose.yml`, `compose.vault.yml`, `compose.host-gateway.yml`, `compose.gateway-sso.yml`, `Dockerfile`, `Makefile`, `UPSTREAMS.md`
- **Not published (gitignored):** `MISSION.md`, `FACTORY.md`, `FACTORY_RULES.md`, `harness/`,
  `.factory/` runtime state, `.archon/workflows/factory/`, and `core-modules/*` — the vendored upstreams are
  cloned by `setup.sh` / `bootstrap.sh` on first run, so provenance stays pinned in [UPSTREAMS.md](UPSTREAMS.md)
  instead of drifting with a copy in git

If you are looking for the safety rules, the holdout contract, or the five YAML DAGs, they live in the
source checkout of the factory — not in this distribution repo.

---

## Documentation

| Document | What it covers |
| --- | --- |
| [web/studio/README.md](web/studio/README.md) | Studio — the vibe-coding web UI: how to run it, configuration, output contract, security posture |
| `scripts/olympus-tui.py` | The builder's terminal front end (`make tui`) — what it reads, the keys, and why it queues rather than planning or generating |
| [docs/stack.md](docs/stack.md) | This platform's role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) (FactoryOps), including SecretOps via Cerulean Vault (KV v2) and the `vault://` reference convention |
| [docs/gateway-sso.md](docs/gateway-sso.md) | Putting the OmniRoute dashboard behind Cerulean Authentik — why the gateway cannot do it natively, and the proxy that does |
| [docs/site-publishing.md](docs/site-publishing.md) | Studio's two kinds of build — an **app** (React + API + SQLite, one container per app) and a **website** (Vite + React, static) — the `app-package`/`app-up`/`app-publish` and `site-package`/`sites-up`/`site-publish` pipelines, how a name reaches a container, and the ONYX (storage/NAS/app-hosting) integration points |
| [UPSTREAMS.md](UPSTREAMS.md) | Vendored upstream mirrors (Archon, AI Software Factory, skills, OmniRoute, archon-cli) — provenance, pinned SHAs, licenses, recovery and re-sync |
| [factory/APP_SPEC_TEMPLATE.md](factory/APP_SPEC_TEMPLATE.md) | The spec format `make new-request` scaffolds and the manufacture step reads |
| `.env.example` | Every knob the stack reads, grouped and commented (gateway, Studio, OIDC, Vault, Cerulean, NPM, Magnate) |
| `Makefile` | The full target list — `make help` |
| `MISSION.md`, `FACTORY.md`, `FACTORY_RULES.md`, `AGENTS.md`, `harness/END-TO-END.md`, `.factory/holdout/HOLDOUT.md` | Scope, operations, safety rules and the holdout contract — **in the source checkout**, gitignored here (see above) |

---

## License

Olympus is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text. Upstream frameworks under `core-modules/` retain their own licenses in-tree.

---

*Olympus · FactoryOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) — one job per platform, platform services consumed by business functions.*
