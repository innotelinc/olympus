#!/usr/bin/env bash
# scripts/manufacture.sh — local app manufacturing (mirrors .github/workflows/olympus-app-builder.yml)
# Usage: ./scripts/manufacture.sh [build-requests/foo.md]
#        SPEC=build-requests/foo.md ./scripts/manufacture.sh
# If no spec given, picks the most recent build-requests/*.md (lexicographically last mtime).
# Output is always ./builds (per .archon/config.yaml factory_settings.output_dir). Ignored by .gitignore.
#
# WHAT CHANGED AND WHY. This used to invoke
# `factory.py run archon-greenfield`, and that command cannot succeed: the factory
# only accepts workflows discovered inside its SHA-pinned Archon source, and it
# refuses every action when that source is dirty. `archon-greenfield` was never part
# of the pinned pack — upstream included — so the name had no implementation to point
# at, in either `make app` or CI. The app builder is therefore a workflow in THIS
# checkout (.archon/workflows/app/greenfield/), run through the Archon CLI directly:
# the factory keeps owning issue → PR, and this owns spec → app.
#
# Env: ARCHON_BIN      the archon CLI to use (else core-modules/archon/bin/archon, else PATH)
#      ARCHON_RUN_ARGS extra args for `archon workflow run` (e.g. --detach)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="${1:-${SPEC:-}}"
WORKFLOW="archon-greenfield"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

if [ -z "$SPEC" ]; then
  # Most recently modified md in build-requests/ excluding README.md
  SPEC="$(ls -t "$ROOT_DIR"/build-requests/*.md 2>/dev/null | grep -v README | head -n 1 || true)"
  SPEC="${SPEC#"$ROOT_DIR"/}"
fi

if [ -z "$SPEC" ] || [ ! -f "$SPEC" ]; then
  echo "manufacture: no spec found. Create one with: make new-request NAME=my-app" >&2
  echo "             or: cp factory/APP_SPEC_TEMPLATE.md build-requests/my-app.md" >&2
  exit 2
fi

say "manufacture: spec: $SPEC"

# --- the workflow --------------------------------------------------------------
# One workflow, one name, and the checkout is the only place it can live.
[ -f "$ROOT_DIR/.archon/workflows/app/greenfield/${WORKFLOW}.yaml" ] \
  || die "manufacture: .archon/workflows/app/greenfield/${WORKFLOW}.yaml is missing from this checkout"

# --- the CLI -------------------------------------------------------------------
# Try the candidates in order and keep the first that actually answers, because a
# path can exist without being usable (an unbuilt checkout, a partial clone) and
# "the binary is present" is not the same claim as "the binary runs".
candidates=()
[ -n "${ARCHON_BIN:-}" ] && candidates+=("$ARCHON_BIN")
candidates+=("$ROOT_DIR/core-modules/archon/bin/archon")
command -v archon >/dev/null 2>&1 && candidates+=("$(command -v archon)")

ARCHON=""
for candidate in "${candidates[@]}"; do
  if [ -x "$candidate" ] && "$candidate" --version >/dev/null 2>&1; then
    ARCHON="$candidate"
    break
  fi
done

if [ -z "$ARCHON" ]; then
  warn "manufacture: no working archon CLI found. Tried: ${candidates[*]:-none}"
  die "Run ./setup.sh (or ./scripts/bootstrap.sh) to install it — see docs/stack.md"
fi
say "manufacture: archon: $ARCHON"

# --- the gateway ----------------------------------------------------------------
# Checked BEFORE the run, not during it: a build that dies 20 minutes in because the
# model gateway was never up has burned the run to learn something a two-second probe
# knows up front.
if [ -n "${OMNIROUTE_BASE_URL:-}" ]; then
  health="${OMNIROUTE_BASE_URL%/v1}/healthz"
  if ! curl -fsS -m 5 "$health" >/dev/null 2>&1; then
    warn "manufacture: gateway is not answering at $health"
    die "Start it (docker compose up -d omniroute, or ./setup.sh) — the build step needs it"
  fi
  say "manufacture: gateway: $health"
else
  warn "manufacture: OMNIROUTE_BASE_URL is unset — the agent will use its own default gateway"
fi

# --- run ------------------------------------------------------------------------
# Foreground by default, so `make app` returns having actually built the app. Pass
# ARCHON_RUN_ARGS=--detach for a long build you would rather poll.
# shellcheck disable=SC2086
"$ARCHON" workflow run "$WORKFLOW" --no-worktree \
  --input spec="$SPEC" ${ARCHON_RUN_ARGS:-}

say "manufacture: done — output under $(sed -n 's/^  output_dir: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$ROOT_DIR/.archon/config.yaml" | head -1 || echo ./builds)"
