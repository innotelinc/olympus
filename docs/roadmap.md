# Olympus roadmap

**Role: FactoryOps** — the one web UI, one terminal UI and one full-stack app
builder for the Innotel Platform Stack. See [stack.md](stack.md) for the role and
[the build-plane convergence](../../../ips/docs/convergence-onyx-olympus-distro-atlas.md)
for the target architecture across ONYX, Olympus, Distro and Atlas.

> **Status.** **0.1 is shipped** — Studio (the one web UI), the Olympus TUI, the
> builder engine (plan → stream → package → run → publish), per-app vhosts via
> Cerulean + NPM, the build runner and the saved-app store. **0.2.0 is open**;
> the convergence track below is the working list.

## 0.1 — done

- [x] **Studio** — Next.js App Router, Authentik OIDC (PKCE + id_token
      verification), plan → stream → six deliveries.
- [x] **The builder** — `project_plan.py` → Archon → Codex → `package-app.py` /
      `package-website.py` → `app-runtime.py` (one container, loopback port, a
      vhost and a published name).
- [x] **The TUI** — `scripts/olympus-tui.py` (`make tui`): queues and streams.
- [x] **Sites** — per-app publish/unpublish over Cerulean DNS/certs + NPM.
- [x] **Secrets** — Cerulean Vault (KV v2), path-scoped token, `vault://`
      references; Infisical is retired and refused.
- [x] **The gateway dashboard** — OmniRoute behind oauth2-proxy + Authentik
      (`gateway.olympus.innotel.us`), server-side sessions in redis.

## 0.2.0 — open

- [x] **Tenancy through Distro's control plane** — per-identity gateway keys,
      quotas and audit, instead of Studio's single static key. Studio
      (`web/studio/lib/controlplane.ts` + `lib/tenancy.ts`) resolves the caller
      to a control-plane account, spends that user's own key (`beginTurn` /
      `finishTurn` in the plan and generate routes), gates on quota per turn
      (fail-open) and writes audit rows for build/publish/export. Distro
      exposes the four `/api/internal/*` endpoints behind
      `CONTROL_INTERNAL_TOKEN`. (convergence §5)
- [ ] **One gateway, one model chain** — Studio points at the shared Group 2
      OmniRoute; the local profile is retired and `make build-model-check`
      guards the transition. (convergence §4.4)
- [ ] **Build-model convergence** — one chain configured once
      ([build-model.md](build-model.md)); the front-door count stays at two.
- [x] **Sign-in hardening** — `make studio-oidc-check` now proves the client
      credentials actually authenticate (`--verify-client`), so an
      `invalid_client` at the token endpoint is diagnosed, not guessed at;
      the provisioning repair also fixes `client_type`/`client_id` in place.

## Not in 0.2

- Distro's builder surface stays retired; Atlas keeps CodeOps (Gitea, Convex,
  CI) and does not become a second execution plane. See the convergence doc.
