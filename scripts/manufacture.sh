#!/usr/bin/env bash
# scripts/manufacture.sh — local app manufacturing (mirrors .github/workflows/olympus-app-builder.yml)
# Usage: ./scripts/manufacture.sh [build-requests/foo.md]
#        SPEC=build-requests/foo.md ./scripts/manufacture.sh
#        REPLACE=1 SPEC=build-requests/foo.md ./scripts/manufacture.sh   # rebuild over an existing app
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
# Env: ARCHON_BIN      the archon CLI to use, an operator override
#      ARCHON_BINARY   the same, under the name `.env` and setup.sh write
#                      (else core-modules/archon/bin/archon, else PATH)
#
# Both names are honoured, and both are new relative to what this script used to
# accept. It read `ARCHON_BIN` only — while `.env` sets `ARCHON_BINARY`, the build
# runner allow-lists `ARCHON_BINARY`, and setup.sh writes `ARCHON_BINARY`. So the
# setting an operator changed was inert, and builds worked only because the
# fallback below happens to resolve to the same file when the CLI is built in the
# checkout. A relative value is taken relative to this repo, so it means the same
# thing whether the script is run from the root or from a subdirectory.
#      ARCHON_RUN_ARGS extra args for `archon workflow run` (e.g. --detach)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="${1:-${SPEC:-}}"
WORKFLOW="archon-greenfield"

# --- the local environment -------------------------------------------------------
# This script is both the runner's executor and the operator's `make app`. The runner
# loads .env itself and passes on only OMNIROUTE_*/ARCHON_*; a hand-run `make app` has
# no such loader, so without this it aims the agent at the gateway carrying no key and
# fails as an auth error.
#
# Only the keys the build actually reads, and only the ones still unset — the
# environment is the authority whenever it says anything, which is the same precedence
# the runner applies to the file. Sourcing the file wholesale (`set -a; . ./.env`, as
# scripts/bootstrap.sh does) would overwrite what the caller passed: measured,
# `OMNIROUTE_MODEL=x make app` silently built with the file's model instead.
if [ -f "$ROOT_DIR/.env" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      OMNIROUTE_*|ARCHON_*) ;;
      *) continue ;;
    esac
    # Trim the quoting .env files sometimes carry.
    value="${value%\"}"; value="${value#\"}"
    value="${value%'}"; value="${value#'}"
    [ -n "$(printenv "$key" 2>/dev/null || true)" ] || export "$key=$value"
  done < "$ROOT_DIR/.env"
fi

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
for configured in "${ARCHON_BIN:-}" "${ARCHON_BINARY:-}"; do
  [ -n "$configured" ] || continue
  case "$configured" in
    /*) candidates+=("$configured") ;;
    *)  candidates+=("$ROOT_DIR/$configured") ;;
  esac
done
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

# --- the script runtime ----------------------------------------------------------
# Every node in this workflow declares `runtime: uv` and Archon runs them through that
# binary. Checked here so a host that never ran setup.sh is told what is missing, rather
# than watching the first node fail before it has read the spec.
if ! command -v uv >/dev/null 2>&1; then
  die "manufacture: the 'uv' runtime is missing, and every $WORKFLOW node needs it.\n          Install: curl -LsSf https://astral.sh/uv/install.sh | sh   (or run ./setup.sh)"
fi
say "manufacture: uv: $(command -v uv)"

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
  # Not fatal: build-app.py falls back to the endpoint this stack publishes the gateway
  # on. The run may still fail on auth, so say what is actually missing.
  warn "manufacture: OMNIROUTE_BASE_URL is unset and .env had no value — the agent will use the default gateway with no key"
fi

# --- replace consent ------------------------------------------------------------
# The load node refuses to manufacture over an app that already exists, so that a
# rebuild cannot quietly destroy the one you were comparing against. Studio's own
# rebuild gives that consent as `{ "replace": true }`; REPLACE=1 is the same consent
# for a hand-run or scripted one. Without it, RETRYING a failed build is impossible:
# the project that failed is the project that has output.
RUN_INPUTS=(--input "spec=$SPEC")
case "$(printf '%s' "${REPLACE:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on)
    RUN_INPUTS+=(--input replace=true)
    warn "manufacture: REPLACE set — a previous ./builds/<app> for this spec is deleted before building"
    ;;
esac

# --- run ------------------------------------------------------------------------
# Foreground by default, so `make app` returns having actually built the app. Pass
# ARCHON_RUN_ARGS=--detach for a long build you would rather poll.
# shellcheck disable=SC2086
"$ARCHON" workflow run "$WORKFLOW" --no-worktree \
  "${RUN_INPUTS[@]}" ${ARCHON_RUN_ARGS:-}

say "manufacture: done — output under $(sed -n 's/^  output_dir: *"\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$ROOT_DIR/.archon/config.yaml" | head -1 || echo ./builds)"
