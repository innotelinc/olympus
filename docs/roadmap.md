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

## Open — 0.2

- [ ] **One gateway, one model chain** — Studio and the factory point at the shared
      Group 2 OmniRoute; the local profile stays retired and `make build-model-check`
      guards the transition. (convergence §4.4)
- [ ] **Build-model convergence** — one chain configured once
      ([build-model.md](build-model.md)); the front-door count stays at two.

## Next — 0.3

- [ ] **Autonomy L1 — dispatch.** Arm the dispatcher after a watched lap:
      queued accepted issues → watched implement runs, still at a manual gate.
- [ ] **Publish-time observability.** A publish that is not checked is a publish
      nobody knows about — fold `site-check` / `gateway-edge-check` evidence into
      the panel and the job record.
- [ ] **Operator actions in the panel** — deliberately read-only today; anything
      that writes is a separate decision with its own gate.

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
