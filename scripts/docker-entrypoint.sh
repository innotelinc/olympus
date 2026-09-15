#!/usr/bin/env bash
# scripts/docker-entrypoint.sh — Olympus container entrypoint.
# Mirrors local `make setup && make app` / bootstrap.sh, but container-native:
#   - respects TELEGRAM_BOT_TOKEN / OMNIROUTE_* from env (never committed)
#   - starts OmniRoute in the background if bundled under core-modules/omniroute
#   - optionally runs a manufacture for SPEC on boot (SPEC env or arg)
#   - leaves the container running so `make docker-logs` / `docker exec` work
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m==>\033[0m %s\n' "$*" >&2; }

# git inside container (safe.directory is set in Dockerfile)
git config --global --add safe.directory /app 2>/dev/null || true
git config core.hooksPath .githooks 2>/dev/null || true

# Ensure local dev dirs exist (setup.sh also does this; cheap to repeat)
mkdir -p builds build-requests .archon/cache factory 2>/dev/null || true

# OmniRoute: REPORT, never start one.
#
# This used to start a bundled gateway from core-modules/omniroute whenever
# OMNIROUTE_BASE_URL did not answer — and that made two OmniRoutes possible on one
# host. The second one is never useful and is sometimes actively harmful: it has no
# provider connections, so it answers on its free pool and looks like a working
# gateway, while the machine that matters (the build runner, the SSO proxy, anything
# else pointed at a gateway) may find *it* instead of the deployment's real
# gateway — with none of the credentials. The gateway is the shared Group 2
# service now (`2-voice/`, mesh 10.10.2.1), so the single-owner rule holds by
# construction: exactly one OmniRoute exists, and this container is not it.
OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://10.10.2.1:20128/v1}"
if curl -fsS -m 5 "${OMNIROUTE_BASE_URL%/v1}/healthz" >/dev/null 2>&1; then
  say "OmniRoute reachable at ${OMNIROUTE_BASE_URL}"
else
  warn "OmniRoute is NOT reachable at ${OMNIROUTE_BASE_URL}"
  warn "  start it on its own host:  docker compose -f 2-voice/docker-compose.yml up -d omniroute"
  warn "  or accept that this container cannot reach a gateway — it will not start a second one"
fi

# If factory binary exists and a SPEC was requested, run manufacture once on boot.
SPEC="${SPEC:-${1:-}}"
if [ -n "${SPEC:-}" ]; then
  say "Boot manufacture: $SPEC"
  bash scripts/manufacture.sh "$SPEC" || warn "manufacture failed for $SPEC"
elif [ -n "${AUTO_MANUFACTURE:-}" ]; then
  # Optional auto-mode: if any build-requests/*.md exists, manufacture the newest
  if ls build-requests/*.md >/dev/null 2>&1; then
    say "AUTO_MANUFACTURE=1 — manufacturing most recent build-requests/*.md"
    bash scripts/manufacture.sh || warn "auto manufacture failed"
  fi
fi

# Doctor (non-blocking) — mirrors bootstrap step 5
if [ -f factory/doctor.py ]; then
  say "factory/doctor"
  python3 factory/doctor.py 2>&1 | head -n 80 || true
fi

# Keep the container alive (so docker compose ps shows healthy + you can exec).
# If the caller passed a command (e.g. docker run olympus make app), run it instead.
if [ $# -gt 0 ] && [ -n "${1:-}" ] && [ "$1" != "${SPEC:-}" ]; then
  say "exec: $*"
  exec "$@"
fi
if [ -n "${COMMAND:-}" ]; then
  say "COMMAND: $COMMAND"
  exec bash -c "$COMMAND"
fi

say "Olympus ready — container will stay up (sleep infinity). Use 'docker exec -it olympus make app' or mount build-requests."
exec sleep infinity
