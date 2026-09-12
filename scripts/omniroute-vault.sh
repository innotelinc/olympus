#!/usr/bin/env bash
#
# Start OmniRoute with its initial password resolved from Cerulean Vault.
#
# Replaces the Infisical-backed launcher: same job, same shape, but the secret
# comes from HashiCorp Vault KV v2 at <VAULT_PREFIX>/<VAULT_PATH> — the
# SecretOps layer the platform actually runs.
#
# Requires: OMNIROUTE_PORT / OMNIROUTE_HOST (optional), VAULT_ADDR,
#           VAULT_TOKEN (or VAULT_TOKEN_FILE), VAULT_PREFIX (default: cerulean),
#           VAULT_PATH (default: olympus), and the OmniRoute CLI.
set -euo pipefail

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

: "${VAULT_ADDR:?Set VAULT_ADDR in your shell or local environment}"

VAULT_PREFIX="${VAULT_PREFIX:-cerulean}"
VAULT_PATH="${VAULT_PATH:-olympus}"
OMNIROUTE_PORT="${OMNIROUTE_PORT:-20128}"
OMNIROUTE_HOST="${OMNIROUTE_HOST:-0.0.0.0}"
SECRET_KEY="${VAULT_SECRET_KEY:-INITIAL_PASSWORD}"

if [ -z "${VAULT_TOKEN:-}" ]; then
  : "${VAULT_TOKEN_FILE:?Set VAULT_TOKEN, or VAULT_TOKEN_FILE to a file containing one}"
  [ -r "$VAULT_TOKEN_FILE" ] || { printf '%s\n' "Cannot read VAULT_TOKEN_FILE: $VAULT_TOKEN_FILE" >&2; exit 1; }
  VAULT_TOKEN="$(tr -d '\r\n' < "$VAULT_TOKEN_FILE")"
fi
[ -n "$VAULT_TOKEN" ] || { printf '%s\n' 'Vault token is empty.' >&2; exit 1; }

command -v omniroute >/dev/null 2>&1 || { printf '%s\n' 'OmniRoute CLI is required.' >&2; exit 1; }

curl_args=(-sS --fail-with-body -m 15 -H "X-Vault-Token: $VAULT_TOKEN")
if [ "${VAULT_SKIP_VERIFY:-0}" = "1" ]; then curl_args+=(-k); fi
if [ -n "${VAULT_NAMESPACE:-}" ]; then curl_args+=(-H "X-Vault-Namespace: $VAULT_NAMESPACE"); fi

# KV v2 nests the payload under data.data, so read the envelope and pull the key.
envelope="$(curl "${curl_args[@]}" "$VAULT_ADDR/v1/$VAULT_PREFIX/data/$VAULT_PATH")" || {
  printf '%s\n' "Could not read $VAULT_PREFIX/$VAULT_PATH from Vault at $VAULT_ADDR." >&2
  printf '%s\n' 'Check the token scope, and run scripts/vault-bootstrap.py if the path is new.' >&2
  exit 1
}

# Never echo the secret: pass it through the environment, not an argument list.
INITIAL_PASSWORD="$(
  printf '%s' "$envelope" | SECRET_KEY="$SECRET_KEY" python3 -c '
import json, os, sys
try:
    payload = json.load(sys.stdin).get("data", {}).get("data", {})
except Exception:
    sys.exit("Vault returned something that is not a KV v2 envelope.")
value = payload.get(os.environ["SECRET_KEY"])
if not value:
    sys.exit("Vault path has no key named " + os.environ["SECRET_KEY"])
print(value)'
)" || exit 1

if [ "${#INITIAL_PASSWORD}" -lt 8 ]; then
  printf '%s\n' 'The stored password is too short (< 8 characters).' >&2
  exit 1
fi
export INITIAL_PASSWORD

echo "Setting OmniRoute password from Vault ($VAULT_PREFIX/$VAULT_PATH)..."
omniroute setup --password "$INITIAL_PASSWORD" --non-interactive

echo "Starting OmniRoute on port ${OMNIROUTE_PORT}..."
exec env OMNIROUTE_SERVER_HOST="$OMNIROUTE_HOST" REQUIRE_API_KEY=true \
  omniroute serve --port "$OMNIROUTE_PORT" --daemon --no-open --no-tray
