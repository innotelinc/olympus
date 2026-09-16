#!/usr/bin/env bash
# secret-scan.sh — pre-commit secret gate.
#
# Scans the ADDED lines of every staged file through the repo's
# scripts/secret-scan.py, labelled with the file name so test fixtures keep
# their relaxation (and a finding reports the real path). Only added lines are
# scanned — the same scope as the sibling attribution guard — while CI's
# `secret-scan.py` job keeps covering the whole tracked tree.
#
# Fail-open by design: a missing python3 or scanner warns and skips rather
# than blocking every commit on a broken toolchain. Findings themselves always
# block.
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "$0")" && pwd)"
scanner="${HOOK_DIR}/../scripts/secret-scan.py"

if ! command -v python3 >/dev/null 2>&1; then
  echo "secret-scan: python3 not found — skipping (toolchain missing)" >&2
  exit 0
fi
if [ ! -f "$scanner" ]; then
  echo "secret-scan: scanner missing at scripts/secret-scan.py — skipping" >&2
  exit 0
fi

staged="$(git diff --cached --name-only --diff-filter=ACMR)"
[ -n "$staged" ] || exit 0

rc=0
while IFS= read -r f; do
  [ -n "$f" ] || continue
  [ -f "$f" ] || continue
  # Only the added lines (never unchanged context) — matches the attribution
  # guard's scope; '+' markers are stripped so value-shape rules see the raw text.
  added="$(git diff --cached --no-color -U0 -- "$f" 2>/dev/null | grep '^+' | grep -v '^+++' | sed 's/^+//')"
  [ -n "$added" ] || continue
  if ! printf '%s\n' "$added" | python3 "$scanner" --stdin "$f"; then
    echo "secret-scan: commit blocked — credential-shaped content in $f (see above)." >&2
    rc=1
  fi
done <<< "$staged"

exit $rc
