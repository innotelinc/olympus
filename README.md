<div align="center">

[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-brightgreen.svg)](LICENSE)

# 🏭 Olympus — The AI Software Factory

**The repository-local automation platform that turns GitHub issues into validated pull requests — observable, gated, and self-hosted.**

Olympus is a deterministic issue → PR factory for the repo it lives in: Archon workflows (YAML DAGs) drive triage → plan → implement → independent validation → controlled merge, with protected-path enforcement, required markers, non-zero evidence counts, and stop controls. One clone, one command, and a coding agent is wired to your OmniRoute gateway — ready to run factory workflows without re-implementing identity, secrets, billing, or storage.

[![CI](https://github.com/innotelinc/olympus/actions/workflows/ci.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/ci.yml)
[![Conformity](https://github.com/innotelinc/olympus/actions/workflows/conform.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/conform.yml)
[![Pages](https://github.com/innotelinc/olympus/actions/workflows/pages.yml/badge.svg)](https://github.com/innotelinc/olympus/actions/workflows/pages.yml)

</div>

---

## Why Olympus

| Problem | Olympus answer |
| --- | --- |
| Issues stall between triage and a trustworthy PR | Deterministic factory workflows (prime → implement → validate, plus regress and triage) with independent validation |
| Automation that silently skips checks or walks past missing evidence | Non-zero evidence counts, required markers, ratchet floors, and fail-closed behavior are enforced |
| Agents that touch governance or secrets as ordinary work | Protected paths, secret-shaped-file blocks, and bounded PR size — fix the source, never the harness |
| Heavy model inference that heats the local CPU | 100% of heavy inference routed to free cloud models via the OmniRoute gateway (Codex `auto/coding` + Hermes 3 via OpenRouter), local Ollama bypassed |
| No visibility into autonomy, blockers, or held work | `factory/doctor.py` and `factory/trigger.py --status` report readiness, autonomy, evidence, and what is held |

> **About Olympus** — a repository-local AI software factory built with three upstream frameworks under `core-modules/` — [OmniRoute](https://github.com/inotex/omniroute) (cloud routing gateway), [Archon](https://github.com/JohanLi233/archon) (YAML DAG workflow engine), and the AI Software Factory (SDLC scheduling and task consumer). The local CPU stays cool because the coding brain (**Codex** via OmniRoute `auto/coding`, Responses API) and the Telegram frontend (**Hermes 3 70B** via OpenRouter Free) never load weights locally — they run behind the gateway at `http://localhost:20128/v1` (Telegram at `nousresearch/hermes-3-llama-3-70b:free`). **Landing page:** [innotelinc.github.io/olympus](https://innotelinc.github.io/olympus)

---

## What it is

- **Owns:** repository automation (issue → watched workflow → open PR), validation and safety (protected paths, required markers, evidence counts, stop controls), and operational visibility (`factory/doctor.py`, `factory/trigger.py`)
- **Owns:** the five Archon factory workflows and the harness as the definition of "working" — the harness is never edited to make a check pass
- **Provides:** a one-command agent-ready clone (`bash scripts/bootstrap.sh`) and an interactive Telegram surface for issue → fix laps
- **Provides:** [Studio](web/studio/) — the browser vibe-coding surface: describe an app in plain language, watch it build, iterate on it, all against the same OmniRoute gateway (`make studio-dev`)
- **Fast lane:** Issue → Fix PR in ~35 minutes including autonomous code review
- **Classification:** **FactoryOps** — see [docs/stack.md](docs/stack.md)

---

## Quick start

### Local (clone → ready)

```bash
git clone https://github.com/innotelinc/olympus.git
cd olympus
bash scripts/bootstrap.sh
```

### Docker (one command)

```bash
git clone https://github.com/innotelinc/olympus.git
cd olympus
cp .env.example .env   # set TELEGRAM_BOT_TOKEN, OMNIROUTE_* if you have a gateway
make docker-up          # builds image ghcr.io/innotelinc/olympus:local + starts container
make docker-logs        # tail factory + gateway logs
```

Then inside the container (or via `make docker-app`):

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

See `MISSION.md`, `FACTORY.md`, and `FACTORY_RULES.md` in the source repo for scope, operations, and safety rules, and `harness/END-TO-END.md` plus `.factory/holdout/HOLDOUT.md` for the holdout contract.

---

## Documentation

| Document | What it covers |
| --- | --- |
| [UPSTREAMS.md](UPSTREAMS.md) | Vendored upstream mirrors (Archon, AI Software Factory, skills, OmniRoute) — provenance, SHAs, licenses, recovery || [docs/stack.md](docs/stack.md) | This platform's role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) (FactoryOps) | 
| [web/studio/README.md](web/studio/README.md) | Studio — the vibe-coding web UI: how to run it, configuration, output contract, security posture | 
| `MISSION.md` | What Olympus is and is not — the factory's scope (in the source repo) |
| `FACTORY.md` | How the five components are built, autonomy ladder (in the source repo) |
| `FACTORY_RULES.md` | Safety rules: protected files, gates, caps (in the source repo) |
| `AGENTS.md` | Conventions for agents working in this repo (in the source repo) |
| `harness/END-TO-END.md` | Journeys that must pass (in the source repo) |
| `.factory/holdout/HOLDOUT.md` | Holdout the auto-merge rests on (in the source repo) |
| `docs/stack.md` | SecretOps via Cerulean Vault (KV v2), and the `vault://` reference convention |

---

## License

Olympus is licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)**. See [LICENSE](LICENSE) for the full text. Upstream frameworks under `core-modules/` retain their own licenses in-tree.

---

*Olympus · FactoryOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) — one job per platform, platform services consumed by business functions.*
