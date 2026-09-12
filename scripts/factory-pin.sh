#!/usr/bin/env bash
# scripts/factory-pin.sh — pin the factory's Archon integration.
#
# The factory refuses to run without an "integration pin": a record of the exact
# Archon commit it is allowed to drive. Nothing ever created it, so every
# manufacturing run died with:
#
#   Factory refused: Integration pin required. Run factory init --source ...
#
# The required revision is NOT restated here. The factory's own manifest
# (`template/factory/pack.json`) declares `repository` and
# `integration_revision_required`, and `factory init` reads both when given no
# flags — so the factory remains the single source of truth and cannot drift from
# a copy in this repo.
#
# ⚠ `factory init` also installs the factory's managed runtime files into this
# checkout (`factory/*.py`, `harness/`, `.factory/`, `FACTORY.md`, …). In this
# repository `factory/` already holds Olympus's own files and the factory's
# copies REPLACE them — `factory/doctor.py` backs the compose healthcheck, so a
# silent overwrite is not acceptable. This script therefore refuses to run on a
# dirty tree unless FORCE=1, and reports exactly what it replaced.
#
# Usage: ./scripts/factory-pin.sh
# Env:   FACTORY_ARCHON_CACHE  where the pinned checkout is cached
#        ARCHON_REPO           override the source repository (e.g. the mirror)
#        ARCHON_REVISION       override the required revision
#        FORCE=1               proceed with a dirty tree
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FACTORY_DIR="$ROOT_DIR/core-modules/ai-software-factory"
FACTORY_BIN="$FACTORY_DIR/bin/factory.py"
MANIFEST="$FACTORY_DIR/template/factory/pack.json"
SETTINGS="$ROOT_DIR/.factory/consumer.json"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m==>\033[0m %s\n' "$*" >&2; exit 1; }

[ -f "$FACTORY_BIN" ] || die "factory-pin: $FACTORY_BIN not found — run ./setup.sh first (it clones ai-software-factory)"
[ -f "$MANIFEST" ]    || die "factory-pin: $MANIFEST not found — the factory checkout looks incomplete"

CACHE="${FACTORY_ARCHON_CACHE:-$HOME/.cache/factory/archon}"

required_revision() {
  python3 - "$MANIFEST" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("integration_revision_required", ""))
except Exception:
    pass
PY
}

pinned_revision() {
  [ -f "$SETTINGS" ] || return 0
  python3 - "$SETTINGS" <<'PY' 2>/dev/null || true
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("revision", ""))
except Exception:
    pass
PY
}

revision="${ARCHON_REVISION:-$(required_revision)}"
[ -n "$revision" ] || die "factory-pin: the factory manifest declares no integration_revision_required"

current="$(pinned_revision)"
if [ "$current" = "$revision" ] && [ -d "$CACHE/$revision/.git" ]; then
  say "factory-pin: already pinned to ${revision:0:12}"
  exit 0
fi

# init rewrites the factory's managed files in this checkout. Losing a dirty
# working tree to that would be silent and expensive, so check first.
dirty="$(git -C "$ROOT_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)"
if [ -n "$dirty" ] && [ "${FORCE:-0}" != "1" ]; then
  warn "factory-pin: refusing to run — init installs managed files over this checkout,"
  warn "factory-pin: and these tracked files have uncommitted changes:"
  printf '%s\n' "$dirty" | sed 's/^/  /' >&2
  die  "factory-pin: commit or stash them, or re-run with FORCE=1"
fi

if [ -n "$dirty" ]; then
  warn "factory-pin: FORCE=1 with a dirty tree — managed files may be replaced:"
  printf '%s\n' "$dirty" | sed 's/^/  /' >&2
fi

# Snapshot the managed paths so we can report precisely what changed.
before="$(git -C "$ROOT_DIR" status --porcelain | sort || true)"

say "factory-pin: pinning Archon ${revision:0:12} via the factory manifest"
# init resolves the project from `git rev-parse --show-toplevel`, so cwd must be
# the repository root.
cd "$ROOT_DIR"
init_args=(init --cache "$CACHE")
[ -n "${ARCHON_REPO:-}" ]     && init_args+=(--source "$ARCHON_REPO")
[ -n "${ARCHON_REVISION:-}" ] && init_args+=(--revision "$ARCHON_REVISION")
python3 "$FACTORY_BIN" "${init_args[@]}"

after="$(git -C "$ROOT_DIR" status --porcelain | sort || true)"
changed="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") || true)"
if [ -n "$changed" ]; then
  warn "factory-pin: init changed these paths in the checkout:"
  printf '%s\n' "$changed" | sed 's/^/  /' >&2
  warn "factory-pin: review them before committing — do not commit factory scaffolding by accident."
fi

say "factory-pin: ${SETTINGS#$ROOT_DIR/} now pins ${revision:0:12} (cache: $CACHE)"
