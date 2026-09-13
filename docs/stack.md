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
| `web/studio/` | Next.js (App Router) + Authentik OIDC | Browser vibe-coding surface — prompt in, runnable app out, gateway key held server-side; saved apps are scoped per identity (OIDC subject) on the stack's own volume |
| Build runner (`scripts/build-runner.py`) | Python + systemd (`olympus-build-runner.service`) | Executes `make app` for builds Studio queues. Studio's image carries no toolchain, so the queue file is the whole interface — and the runner treats it as untrusted input |

> **Known gateway behaviour — a combo turn pins.** `auto/coding` is a *combo*, and the
> gateway pins a native Codex turn to whichever member served the first turn
> (`pinNativeCodexTurn`, 45-minute TTL, keyed by request body + combo name). When the
> provider behind that member has no credentials the pinned path answers
> `503 No credentials for opencode` — while the *unpinned* path serves the same model
> fine, which is why the first turn of a run can succeed and every turn after it fail.
> Credentials live in `provider_connections` inside the gateway's `storage.sqlite`, and
> it is empty on this deployment: the stack runs entirely on free providers. Two
> consequences worth carrying into any agent work here — name a **concrete** model
> rather than the combo for anything multi-turn (`OMNIROUTE_MODEL_FALLBACK` in
> `.env.example` is the app-builder's case of this), and never read an agent's exit code
> as evidence it did anything, because Codex exits **0** after a refused request.

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
- **Distro (BuilderOps)** — Distro builds apps in the browser; Atlas ships them; both sit behind OmniRoute + Authentik + Magnate. Olympus is the repo-bound automation that validates those outputs.

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

```bash
make studio-token-check                          # remaining days; exit 2 once lapsing
make studio-token-rotate AUTHENTIK_HOST=<host>   # rebuild and re-date it
```

On the deployment host the check runs daily without anyone remembering it:

```bash
scripts/install-token-check-timer.sh             # both timers: 06:17 UTC + after boot
systemctl start olympus-studio-token-check.service   # run the credential check now
systemctl start olympus-vault-renew-check.service    # renew the Vault token now
```

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
