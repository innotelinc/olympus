#!/usr/bin/env bash
# scripts/manufacture.sh — local app manufacturing (mirrors .github/workflows/olympus-app-builder.yml)
# Usage: ./scripts/manufacture.sh [build-requests/foo.md]
#        SPEC=build-requests/foo.md ./scripts/manufacture.sh
# If no spec given, picks the most recent build-requests/*.md (lexicographically last mtime).
# Output is always ./builds (per .archon/config.yaml factory_settings.output_dir). Ignored by .gitignore.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SPEC="${1:-${SPEC:-}}"

if [ -z "$SPEC" ]; then
  # Most recently modified md in build-requests/ excluding README.md
  SPEC="$(ls -t "$ROOT_DIR"/build-requests/*.md 2>/dev/null | grep -v README | head -n 1 || true)"
fi

if [ -z "$SPEC" ] || [ ! -f "$SPEC" ]; then
  echo "manufacture: no spec found. Create one with: make new-request NAME=my-app" >&2
  echo "             or: cp factory/APP_SPEC_TEMPLATE.md build-requests/my-app.md" >&2
  exit 2
fi

echo "==> manufacture: spec: $SPEC"

# Ensure local gateway is up if possible (non-fatal if not)
if [ -d "$ROOT_DIR/core-modules/omniroute" ] && ! curl -fsS -m 2 http://localhost:20128/health >/dev/null 2>&1; then
  if [ -f "$ROOT_DIR/scripts/omniroute-infisical.sh" ]; then
    echo "==> OmniRoute not at http://localhost:20128 — attempting local gateway (non-blocking)" >&2
    # Don't block; the factory will complain verbosely if unreachable.
  fi
fi

# Prefer the CI's argv contract if factory binary exists; otherwise fail clearly.
FACTORY_BIN="$ROOT_DIR/core-modules/ai-software-factory/bin/factory.py"
if [ -f "$FACTORY_BIN" ]; then
  python3 "$FACTORY_BIN" run archon-greenfield \
    --input spec="$SPEC" \
    --output="$ROOT_DIR/builds" \
    --detach --json
else
  echo "manufacture: $FACTORY_BIN not found — run ./setup.sh first (clones ai-software-factory)" >&2
  # Local shim: still stage the spec into builds/ so the operator sees progress outside CI
  mkdir -p "$ROOT_DIR/builds"
  base="$(basename "$SPEC" .md)"
  dest="$ROOT_DIR/builds/$base"
  mkdir -p "$dest"
  cp "$SPEC" "$dest/SPEC.md"
  cat > "$dest/README.md" <<EOF
# $base (manufactured stub)

Spec: \`$SPEC\` — full greenfield run requires \`core-modules/ai-software-factory/bin/factory.py\`.

Run \`./setup.sh\` to fetch the factory, then \`./scripts/manufacture.sh $SPEC\` again.
EOF
  echo "manufacture: stub written to $dest (run ./setup.sh to enable full factory)" >&2
  ls -la "$dest" >&2
  exit 0
fi
