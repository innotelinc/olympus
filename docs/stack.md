# Olympus — Platform Stack Role

**Classification: FactoryOps**

**Role: FactoryOps — issue → validated pull request automation for the repository it lives in.**

Olympus is a repository-local AI software factory. It turns accepted GitHub issues into validated pull requests through automated triage, planning, implementation, independent validation, safety gates, and controlled merging — observable at autonomy level 0 before any unattended operation. This page declares its role in the [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack) — the canonical single-responsibility architecture. The stack is defined in exactly one place; this page links this product to it and states what it owns, consumes, provides, and explicitly does not own.

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
- Infisical — secrets, credentials (gateway keys, tokens)
- Cerulean — trust (DNS/TLS for the operator surfaces where exposed)
- NPM Edge — public routing, TLS termination at the edge (where the operating host is fronted)

## Explicitly does NOT own

- Application business logic — Olympus is factory machinery, not a product domain; it does not ship a user app, media library, billing products, or storage promises
- Identity (Authentik), Secrets (Infisical), Trust/DNS/PKI (Cerulean), Storage (ONYX), or Revenue/Billing (Magnate) — the factory integrates with those instead of re-implementing them
- Its own governance as ordinary issue work — `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`, `factory/**`, `.archon/workflows/factory/**`, `harness/**`, `.factory/locks/**`, `.factory/holdout/**`, CI config, and secret-shaped files are protected

## Service map

| Component | Technology | Job |
|---|---|---|
| `factory/` machinery | Python (gate, guard, merge, state, doctor, trigger) | State machine, safety gates, merging, visibility, scheduler control |
| `harness/` | `ci.py`, `harness.config.json`, journeys, holdout | Definition of “working” — never edited to make a check pass |
| `.archon/workflows/factory/` | Archon YAML workflows | Prime → implement → validate, plus regress and triage |
| `scripts/bootstrap.sh` + `scripts/omniroute-infisical.sh` | bash + OmniRoute CLI | One-command clone-to-ready and gateway launcher |
| Telegram interface | Hermes 3 via OpenRouter Free through OmniRoute | Interactive bot that parses intent into Archon DAG runs |
| Coding brain | Codex (`auto/coding`) via OmniRoute Responses API (`wire_api = "responses"`) | Repository code modifications dispatched by the factory consumer + harness E2E (`omniroute launch-codex -p auto-coding`) |
| `web/studio/` | Next.js (App Router) + Authentik OIDC | Browser vibe-coding surface — prompt in, runnable app out, gateway key held server-side |

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
| Identity | Cerulean's Authentik at `https://auth.cerulean.innotel.us` (platform alias `auth.olympus.innotel.us` where wired) — OIDC provider per service |
| Secrets | Cerulean's Infisical — credentials live in Infisical; `.env` is derived and gitignored |
| Trust / Edge | Cerulean (DNS/certs) and NPM Edge where the hosting host is fronted — managed by Cerulean |
| AI plane | OmniRoute gateway in front of upstream models — Codex via `wire_api = "responses"` (primary), one key per user |

## Integration with other platforms

- **Atlas (CodeOps)** — Olympus is the factory that Atlases the ecosystem's code lives in; Atlas owns the canonical git remote + CI for platform code, while Olympus owns the factory loop inside a given repo.
- **Distro (BuilderOps)** — Distro builds apps in the browser; Atlas ships them; both sit behind OmniRoute + Authentik + Magnate. Olympus is the repo-bound automation that validates those outputs.

## Secrets (Infisical)

Secrets for this platform live in **Infisical** (SecretOps): credentials are imported into an Infisical workspace and the stack's `.env` is derived from it. Enable it with:

```bash
# generate the required keys and add them to .env
openssl rand -base64 32   # INFISICAL_ENCRYPTION_KEY
openssl rand -hex 16      # INFISICAL_AUTH_SECRET
openssl rand -hex 16      # INFISICAL_DB_PASSWORD

# start the SecretOps store (opt-in profile)
make up    # wraps: docker compose -f compose.infisical.yml --profile infisical up -d

# create the workspace, the machine identity, and a token — then write .env
export INFISICAL_ADDR=http://localhost:8088 \
       INFISICAL_ADMIN_EMAIL=you@example.com \
       INFISICAL_ADMIN_PASSWORD=... \
       INFISICAL_ORG_ID=...
python3 scripts/infisical-bootstrap.py
```

`scripts/infisical-bootstrap.py` takes every credential from the environment
(nothing is hardcoded, because this repo is public) and writes `.env` at mode
0600. With `INFISICAL_ADDR` / `INFISICAL_TOKEN` / `INFISICAL_PROJECT_ID` in
`.env`, env values may be `infisical://<name>` references resolved at startup.

## Golden rules

- **Authentik = Identity** · **Infisical = Secrets** · **Cerulean = Trust** · **ONYX = Storage** · **Magnate = Revenue** · **NPM Edge = Edge** — everything else is a business function.
- No platform duplicates another's responsibility.
- No credit in commits, footers, or headers to anyone but the project owner.

---

*Olympus · FactoryOps · [Innotel Platform Stack](https://github.com/innotelinc/innotel-platform-stack)*
