#!/usr/bin/env bash
# scripts/studio-export-dir.sh — make a directory Studio writes into writable by it.
#
# Studio runs as uid 1001 (the image drops privileges on purpose), and a bind
# mount keeps the host's ownership — so a root-owned directory answers 503 no
# matter how Studio is configured, and the operator is left doing a one-time
# chown by hand from a doc. Two directories are in that position:
#
#   build-requests/   "Export to factory" writes a spec here
#   .factory/build-queue/   "Build it" writes a build request here
#
# This does that step, idempotently, so a fresh install is ready before anyone
# clicks either:
#
#   already owned by the container uid  → nothing to do
#   owned by someone else, running root → chown it
#   owned by someone else, not root     → say exactly what to run
#
# The directory is created if missing, which also matters for the queue: compose
# would otherwise create the bind-mount source as root on first `up`, and Studio
# could never write to it.
#
# Usage: ./scripts/studio-export-dir.sh [DIR]
# Env:   STUDIO_UID         uid the Studio container runs as (default 1001)
#        STUDIO_EXPORT_DIR  directory to settle (default ./build-requests)
#        STUDIO_DIR_LABEL   what to call it in the output (default build-requests)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-${STUDIO_EXPORT_DIR:-$ROOT_DIR/build-requests}}"
LABEL="${STUDIO_DIR_LABEL:-build-requests}"
STUDIO_UID="${STUDIO_UID:-1001}"
STUDIO_GID="${STUDIO_GID:-$STUDIO_UID}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==>\033[0m %s\n' "$*" >&2; }

if ! [[ "$STUDIO_UID" =~ ^[0-9]+$ ]]; then
  printf '\033[1;31m==>\033[0m STUDIO_UID must be a numeric uid, got %s\n' "$STUDIO_UID" >&2
  exit 2
fi

mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd)"

owner() { stat -c '%u:%g' "$TARGET"; }

# The common case after the first successful run is a no-op, and this runs from
# setup.sh on every install.
if [ "$(owner)" = "$STUDIO_UID:$STUDIO_GID" ]; then
  say "$LABEL ready — $TARGET owned by $(owner) (Studio's uid)"
  exit 0
fi

if [ "$(id -u)" != "0" ]; then
  warn "$TARGET is owned by $(owner), but Studio writes as uid $STUDIO_UID:."
  warn "Writes will answer 503 until that matches. Re-run as root:"
  warn "  sudo $0 $TARGET"
  exit 1
fi

previous="$(owner)"
chown "$STUDIO_UID:$STUDIO_GID" "$TARGET"
say "$LABEL ready — $TARGET: $previous -> $(owner) so Studio can write"
