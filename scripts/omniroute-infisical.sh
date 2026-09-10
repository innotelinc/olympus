#!/usr/bin/env bash
set -euo pipefail

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${INFISICAL_PROJECT_ID:?Set INFISICAL_PROJECT_ID in your shell or local environment}"

command -v infisical >/dev/null 2>&1 || {
  printf '%s\n' 'Infisical CLI is required: https://infisical.com/docs/cli/usage' >&2
  exit 1
}
command -v omniroute >/dev/null 2>&1 || {
  printf '%s\n' 'OmniRoute CLI is required.' >&2
  exit 1
}

INFISICAL_ENV="${INFISICAL_ENV:-dev}"
INFISICAL_PATH="${INFISICAL_PATH:-/}"
INFISICAL_DOMAIN="${INFISICAL_DOMAIN:-}"
OMNIROUTE_PORT="${OMNIROUTE_PORT:-20128}"
OMNIROUTE_HOST="${OMNIROUTE_HOST:-0.0.0.0}"

infisical_args=(
  run
  --silent
  --env="$INFISICAL_ENV"
  --path="$INFISICAL_PATH"
  --projectId="$INFISICAL_PROJECT_ID"
)
if [ -n "$INFISICAL_DOMAIN" ]; then
  infisical_args+=(--domain="$INFISICAL_DOMAIN")
fi

# Write the inner startup script to a temp file for clean execution.
STARTUP_SCRIPT=$(mktemp /tmp/omniroute-startup-XXXXXX.sh)
trap 'rm -f "$STARTUP_SCRIPT"' EXIT

cat > "$STARTUP_SCRIPT" << 'INNER_EOF'
#!/usr/bin/env bash
set -euo pipefail
: "${INITIAL_PASSWORD:?Infisical secret INITIAL_PASSWORD is required}"
if [ "${#INITIAL_PASSWORD}" -lt 8 ]; then
  echo "Infisical INITIAL_PASSWORD must contain at least 8 characters" >&2
  exit 1
fi
echo "Setting OmniRoute password..."
omniroute setup --password "$INITIAL_PASSWORD" --non-interactive
echo "Starting OmniRoute on port ${OMNIROUTE_PORT:-20128}..."
exec env OMNIROUTE_SERVER_HOST="${OMNIROUTE_HOST:-0.0.0.0}" REQUIRE_API_KEY=true \
  omniroute serve --port "${OMNIROUTE_PORT:-20128}" --daemon --no-open --no-tray
INNER_EOF
chmod +x "$STARTUP_SCRIPT"

exec infisical "${infisical_args[@]}" -- bash "$STARTUP_SCRIPT"
