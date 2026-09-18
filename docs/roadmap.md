# Olympus roadmap

**Role: FactoryOps** — the one web UI, the one terminal UI and the one full-stack
app builder for the Innotel Platform Stack. See [stack.md](stack.md) for the role
and [the build-plane convergence](../../../ips/docs/convergence-onyx-olympus-distro-atlas.md)
for the target architecture across ONYX, Olympus, Distro and Atlas.

> **Status.** **0.1 shipped** — Studio (the one web UI), the Olympus TUI, and the
> builder engine (plan → stream → package → run → publish) with the build runner
> and the saved-app store. **0.2 is in development** and most of it has landed:
> the `/admin` deployment panel, plan-before-build, Preview It, the gateway door
> moved to one address, and tenancy through Distro's control plane. The open items
> are the gateway/model convergence, below.

## Shipped — 0.1, the builder

- [x] **Studio** — Next.js App Router, Authentik OIDC (PKCE + `id_token`
      verification), the plan-first build, saved apps per identity.
- [x] **The builder engine** — `project_plan.py` → Archon → Codex →
      `package-app.py` / `package-website.py` → `app-runtime.py` (one container,
      a loopback port, a vhost and a published name), packaged through the
      BuildKit-aware `scripts/buildx`.
- [x] **The TUI** — `scripts/olympus-tui.py` (`make tui`): queues and streams the
      same builder the browser drives.
- [x] **Sites and apps** — per-name publish/unpublish over Cerulean DNS/certs +
      NPM, one wildcard certificate, one generated vhost per app.
- [x] **Secrets** — Cerulean Vault (KV v2), path-scoped token, `vault://`
      references; Infisical is retired and refused.
- [x] **The gateway dashboard** — OmniRoute behind oauth2-proxy + Authentik
      (`gateway.olympus.innotel.us`), server-side sessions in redis.

## Shipped — 0.2, the operator surface

- [x] **`/admin` — the deployment panel** — every failure this deployment has had
      already had a script that diagnosed it (`doctor.py`, `gateway-edge-check.py`,
      `build-model-check.py`, `build-runner.py --list`) and none was reachable from
      a browser. `/admin` is that as a read-only page: the checks, the gateway door
      and its latency, the runner heartbeat and queue, recent jobs, built apps, and
      the access/tenancy posture — gated by `OLYMPUS_ADMIN_GROUPS`.
- [x] **Plan before build** — a turn decides the stack, the install/build/start
      commands, the port and the files, and it is shown for confirmation before any
      code is written (`lib/plan.ts`, `scripts/project_plan.py`). The hardcoded
      React + Node/SQLite scaffold is gone.
- [x] **Live draft preview** — render the files currently arriving from the model in
      a sandboxed frame, including the block in flight, without queueing a delivery
      or registering a name.
- [x] **Preview It** — package the files on screen, run them, and frame the running
      project on `<slug>-preview.<suffix>` — a real runtime under a name of its own,
      without registering the project's name.
- [x] **One gateway door** — `OMNIROUTE_BASE_URL` points at the Authentik SSO proxy
      on `:20129` everywhere; the gateway's own `:20128` stays on its host's loopback
      and bridge, `/v1` is refused on the public name, and `make gateway-auth-mode`
      makes Authentik the only gate. The panel checks the address the way the estate
      documents it.
- [x] **Tenancy through Distro's control plane** — per-identity gateway keys, quotas
      and audit instead of Studio's single static key (`lib/controlplane.ts` +
      `lib/tenancy.ts`): `beginTurn` / `finishTurn` in the plan and generate routes,
      a fail-open quota check per turn, and audit rows for build/publish/export.
- [x] **Build runner hardening** — the account is probed (bubblewrap needs user
      namespaces): `olympus-builder` where they work, root where they do not, always
      sandboxed; the queue is treated as untrusted input and the agent's process
      group is what a cancel signals.
- [x] **Sign-in hardening** — `make studio-oidc-check` proves the client credentials
      authenticate (`--verify-client`), so an `invalid_client` is diagnosed rather
      than guessed at; the provisioning repair fixes `client_type`/`client_id` in
      place.
- [x] **Studio UI** — the composer, the plan card, the streamed code view, the
      build/preview status and history, the deployment panel and the saved-app
      library on one surface.

## Reliability patch — 0.2.1

- [x] **Runner delivery crash fixed** — preview/publish error paths now return the
      complete delivery tuple, so an edge or packaging failure cannot crash the
      queue daemon and strand every later job behind a `.running.json` marker.
- [x] **Preview credentials fixed** — delivery commands receive the scoped Cerulean
      service key and `SITE_*` runtime settings without exposing them to the coding
      agent. Preview can now register its `-preview` edge name after the container
      starts.
- [x] **Empty-agent failures remain explicit** — the greenfield DAG refuses an empty
      artifact rather than reporting a false success; model capacity/tool-call
      failures are recorded in the build log for retry or model-chain repair.

## Open — 0.2

- [x] **One gateway, one model chain** — Studio and the factory point at the shared
      Group 2 OmniRoute; the local profile stays retired and `make build-model-check`
      guards the transition. The DAG now reads the checkout configuration and keeps
      Archon's provider environment from overriding the explicit Codex gateway.
      (convergence §4.4)
- [ ] **Build-model convergence** — one chain configured once
      ([build-model.md](build-model.md)); the front-door count stays at two. The
      remaining work is selecting a provider with sustained tool-call capacity rather
      than relying on free-tier cooldowns.

## Current reliability status — 17 September 2026

- [x] **DAG empty-output guard** — `build-app.py` retries only while the artifact is
      empty, records agent output, and `verify-app.py` refuses an empty project rather
      than allowing a false success. The installer now probes the complete Codex
      bubblewrap namespace (`user + network`) so it selects root when an unprivileged
      account cannot create the network namespace. A retry with only a stale workflow
      `plan.json` now reuses the directory instead of being blocked by `load-spec`.
- [x] **Runner pickup and preview delivery** — the host runner is active, queued jobs
      are claimed, packaged, started, and preview names are registered through the
      Cerulean service key. Delivery now takes the checked-out `CERULEAN_*`, `SITE_*`,
      and `OLYMPUS_*` settings instead of a stale service environment. Verified live
      with `resume-generator`: build succeeded and its preview returned HTTP 200.
- [x] **Estate capacity pass and nightly cleanup (17 September 2026)** — the host's
      leftover smoke and sample app containers were stopped (`olympus-app-runner-smoke`,
      `olympus-app-untitled-app`), reclaiming build cache and unreferenced images
      (10 → 8.2 GB). `scripts/docker-cleanup.sh` (mirrored from ips, canonical there)
      now runs nightly at 04:17 via cron: build cache with a 2 GB floor, dangling and
      unreferenced images, containers exited for more than a day, and oversized logs —
      never volumes, and never a same-day parked container.
- [ ] **Provider capacity** — a configured model can still return HTTP 429 or fail to
      call tools. Planning now retries transient 502/503/504 gateway failures with a
      short bounded backoff, while skipping known cooldown responses. The latest build
      succeeded after gateway recovery; `make build-model-check` remains the guard
      before selecting a primary model with sustained capacity.

## Next — 0.3

- [ ] **Autonomy L1 — dispatch.** Arm the dispatcher after a watched lap:
      queued accepted issues → watched implement runs, still at a manual gate.
      The unblocked prerequisite list is now short: the runner picks up, the DAG
      completes, previews publish and answer 200 — dispatch has no infrastructure
      blocker left, only the gate policy and the watched lap itself.
- [ ] **Publish-time observability.** A publish that is not checked is a publish
      nobody knows about — fold `site-check` / `gateway-edge-check` evidence into
      the panel and the job record. Preview edge registration should use the same
      evidence and expose a retry action when the app is running but Cerulean is
      unavailable.
- [ ] **Operator actions in the panel** — deliberately read-only today; anything
      that writes is a separate decision with its own gate.
- [ ] **Build-plane quotas surfaced per project.** Distro resolves the identity
      and holds the quota decision; the job record should show the quota state
      alongside the build state so a 429 mid-build is visible in the panel, not
      only in the log tail.
- [ ] **Retire the parked sample apps for good.** The capacity pass stopped
      `olympus-app-runner-smoke` and `olympus-app-untitled-app`; give the runner a
      `--prune-samples` mode (and the panel a row for them) so finished sample
      containers are cleaned up by the job that started them.

## Later

- [ ] **Autonomy L2 — validate.** Independent validation on a separate branch and
      deterministic merge after gate agreement.
- [ ] **Autonomy L3 — holdout.** Hidden holdout + the mutation ratchet as the
      auto-merge contract.
- [ ] **ONYX storage (on hold).** A published site is files that want to live on the
      NAS and a name that wants to be a first-class ONYX surface. Nothing calls ONYX
      today; [site-publishing.md](site-publishing.md) keeps the research under an
      explicit on-hold heading.

## Autonomy ladder

Autonomy is earned, not configured: each rung needs a watched lap and explicit
evidence before it is turned on. The factory runs at **L0** by default.

| Rung | Name | What it means | State |
| --- | --- | --- | --- |
| **L0** | Manual | Every lap is started by a human; nothing unattended | current |
| **L1** | Dispatch | The dispatcher queues accepted issues into watched implement runs | 0.3 |
| **L2** | Validate | Independent validation runs on its own branch; merge follows gate agreement | later |
| **L3** | Holdout | The hidden holdout + mutation ratchet become the auto-merge contract | later |

## Not in this roadmap

- Distro's builder surface stays retired; Atlas keeps CodeOps (Gitea, Convex, CI)
  and does not become a second execution plane. See the convergence doc.
- `make gateway-vault-check` remains the drift check for the gateway's provider
  connections — a capability kept, not a milestone.

## 0.3 progress notes (2026-09-18)

- [x] **Pipeline made visible both ways**: the deployment panel now shows the
      delivery flow (queued → building → verified → live) with Distro named
      as the downstream control plane — generation traffic is metered and
      capped there, and the queue is mirrored into Distro's console. Deployed
      and verified on .50.
- [x] **Alert estate repaired after the .50 reinstall**: TELEGRAM_BOT_TOKEN
      (placeholder) and TELEGRAM_CHAT_ID (empty) restored from the estate
      identity; gateway edge/backup timers moved to .46 where the gateway
      actually runs; VAULT_ADDR points at the platform Vault and the
      path-scoped token file is back, so `studio-token-alert` and
      `vault-renew-alert` run green and send to the Telegram channel again.
- [x] **`.env` hygiene**: multi-word values now quoted (bash-sourced tooling
      was executing `email` as a command); `.env.bak*` gitignored.
