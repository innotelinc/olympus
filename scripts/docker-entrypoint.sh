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

# OmniRoute: if a gateway is expected at OMNIROUTE_BASE_URL (default inside compose: http://omniroute:20128),
# try to ensure it's up — either it's an external service or the bundled one under core-modules/omniroute.
OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://localhost:20128}"
if curl -fsS -m 2 "${OMNIROUTE_BASE_URL}/health" >/dev/null 2>&1; then
  say "OmniRoute reachable at ${OMNIROUTE_BASE_URL}"
else
  if [ -d core-modules/omniroute ] && [ -f core-modules/omniroute/package.json ]; then
    warn "OmniRoute not at ${OMNIROUTE_BASE_URL} — starting bundled gateway in background"
    (cd core-modules/omniroute && npm run start >/tmp/omniroute.log 2>&1 &)
    for _ in $(seq 1 15); do
      if curl -fsS -m 2 "${OMNIROUTE_BASE_URL}/health" >/dev/null 2>&1; then
        say "OmniRoute now reachable at ${OMNIROUTE_BASE_URL}"
        break
      fi
      sleep 2
    done
    if ! curl -fsS -m 2 "${OMNIROUTE_BASE_URL}/health" >/dev/null 2>&1; then
      warn "bundled OmniRoute did not become healthy; continuing — see /tmp/omniroute.log"
    fi
  else
    warn "OmniRoute not at ${OMNIROUTE_BASE_URL} and no bundled gateway — set OMNIROUTE_BASE_URL to an external gateway"
  fi
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
