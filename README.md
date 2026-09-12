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
| **Studio** | `web/studio/` — the browser vibe-coding surface. Prompt → streamed files → live preview, Authentik OIDC sign-in, sandboxed output, saved apps scoped to the signed-in identity, and an **Export to factory** path that writes a `build-requests/` spec from a saved build (`make app SPEC=…`, or commit it and let CI manufacture). Runs on its own port, in the stack (`docker compose up -d studio`) or standalone (`make studio-dev`). |
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

# or inside the stack
docker compose up -d studio
```

Studio needs a gateway and (optionally) OIDC. `make setup` scaffolds `.env`; the keys that matter are `OMNIROUTE_BASE_URL` + `OMNIROUTE_API_KEY` for generation, and `OIDC_ISSUER_URL` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_REDIRECT_URI` / `STUDIO_SESSION_SECRET` for sign-in. **Auth stays off until it is configured** — run `make studio-oidc` (or `python3 scripts/authentik-studio-app.py`) to create or repair the Cerulean Authentik application and provider for it. See [web/studio/README.md](web/studio/README.md) for the full contract.

Generated apps are saved per signed-in identity: the OIDC subject decides whose library a build lands in, so a reload or a rebuild no longer loses it. In the stack the library is the `studio-data` volume (`STUDIO_DATA_DIR` points at it).

Then manufacture an app from a spec:

```bash
make new-request NAME=my-todo   # → build-requests/my-todo.md
$EDITOR build-requests/my-todo.md
make app                         # local — or: make docker-app SPEC=build-requests/my-todo.md
make builds                       # list ./builds (volume, gitignored)
```

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
| Environment | `.env.example` → `.env` | `make setup` never overwrites an existing `.env`; new keys are seeded on upgrade |
| Gateway | `OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY` | Remote gateway? point the URL at it. Publish it on loopback only? add `compose.host-gateway.yml` |
| SecretOps | `compose.vault.yml` + `scripts/vault-bootstrap.py` | Cerulean Vault (KV v2). `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_PREFIX`; values may be `vault://<name>` references resolved at startup. Tokens are scoped to this stack's own path |
| Identity | `scripts/authentik-studio-app.py` | Creates the Studio application/provider in Cerulean Authentik; Studio is public until OIDC is set |
| Trust / edge | `scripts/` (NPM + Cerulean) | DNS and TLS for the public names are managed through Cerulean and NPM Edge; see [docs/stack.md](docs/stack.md) |
| Upgrade | `git pull && make setup` | Idempotent; `scripts/factory-pin.sh` pins the factory's upstream SHAs |
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
  `docker-compose.yml`, `compose.vault.yml`, `compose.host-gateway.yml`, `Dockerfile`, `Makefile`, `UPSTREAMS.md`
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
| [docs/stack.md](docs/stack.md) | This platform's role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) (FactoryOps), including SecretOps via Cerulean Vault (KV v2) and the `vault://` reference convention |
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
