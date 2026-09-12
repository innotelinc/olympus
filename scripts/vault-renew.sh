#!/usr/bin/env bash
#
# Keep this stack's Cerulean Vault token from expiring.
#
# Olympus authenticates to Vault with a *periodic* token scoped to its own path
# (the `olympus` policy — see docs/stack.md). A periodic token never has to
# expire: every renewal resets its TTL back to the full period (32 days), so it
# can be kept alive indefinitely. But it *does* lapse if nothing renews it inside
# that window, and the platform's own renewal loop only touches the platform's
# shared token. This script is the other half of that story.
#
# Renewal needs no privileged access: `auth/token/renew-self` acts on the token
# that presents it, so this runs with the same unprivileged token it renews.
#
# Requires: VAULT_ADDR, and VAULT_TOKEN (or VAULT_TOKEN_FILE).
#
#   bash scripts/vault-renew.sh          # renew, then report the new TTL
#   bash scripts/vault-renew.sh --check  # report only, never renew
#
# Run it at least once per period. `--check` is safe to wire into monitoring: it
# exits non-zero once the token is no longer renewable, so a dead token is caught
# before it breaks a build instead of during one.
set -euo pipefail

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

mode="renew"
case "${1:-}" in
  --check|check) mode="check" ;;
  -h|--help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  "") ;;
  *)
    printf '%s\n' "Unknown argument: $1 (expected --check, or nothing)" >&2
    exit 2
    ;;
esac

: "${VAULT_ADDR:?Set VAULT_ADDR in your shell or local environment}"

# Same resolution order as scripts/omniroute-vault.sh and the Vault CLI itself:
# an explicit VAULT_TOKEN wins, otherwise read the file.
if [ -z "${VAULT_TOKEN:-}" ]; then
  : "${VAULT_TOKEN_FILE:?Set VAULT_TOKEN, or VAULT_TOKEN_FILE to a file containing one}"
  [ -r "$VAULT_TOKEN_FILE" ] || {
    printf '%s\n' "Cannot read VAULT_TOKEN_FILE: $VAULT_TOKEN_FILE" >&2
    exit 1
  }
  VAULT_TOKEN="$(tr -d '\r\n' < "$VAULT_TOKEN_FILE")"
fi
[ -n "$VAULT_TOKEN" ] || { printf '%s\n' 'Vault token is empty.' >&2; exit 1; }

curl_args=(-sS --fail-with-body -m 15 -H "X-Vault-Token: $VAULT_TOKEN")
if [ "${VAULT_SKIP_VERIFY:-0}" = "1" ]; then curl_args+=(-k); fi
if [ -n "${VAULT_NAMESPACE:-}" ]; then curl_args+=(-H "X-Vault-Namespace: $VAULT_NAMESPACE"); fi

if [ "$mode" = "check" ]; then
  endpoint="auth/token/lookup-self"
  method="GET"
else
  endpoint="auth/token/renew-self"
  method="POST"
fi

# Write to a file rather than a variable: the response is parsed, never printed,
# and some Vault replies echo a token back in the body.
response="$(mktemp)"
trap 'rm -f "$response"' EXIT

if ! curl "${curl_args[@]}" -X "$method" "$VAULT_ADDR/v1/$endpoint" > "$response"; then
  printf '%s\n' "Vault refused to $mode the token at $VAULT_ADDR." >&2
  printf '%s\n' 'The token is expired, revoked, or not permitted to renew itself.' >&2
  printf '%s\n' 'Re-mint it with the platform root token, then reinstall it:' >&2
  printf '%s\n' '  vault token create -orphan -policy=olympus -period=768h' >&2
  printf '%s\n' "  <token> > $VAULT_TOKEN_FILE   # then: chmod 600" >&2
  printf '%s\n' 'See the Secrets section of docs/stack.md.' >&2
  exit 1
fi

# Heredoc (not `-c`) so the parser can use both quote styles freely — and so the
# token echoed back in some renewal responses is parsed, never printed.
python3 - "$mode" "$response" <<'PY'
import json
import sys

mode, path = sys.argv[1], sys.argv[2]
try:
    with open(path) as handle:
        doc = json.load(handle)
except Exception:
    sys.exit("  Vault returned something that is not JSON.")

if doc.get("errors"):
    detail = "; ".join(e for e in doc["errors"] if e) or "(no detail)"
    sys.exit(f"  Vault error: {detail}")

if mode == "check":
    data = doc.get("data") or {}
    ttl = data.get("ttl")
    period = data.get("period")
    renewable = data.get("renewable")
    print(f"  policies:   {data.get('policies')}")
    print(f"  ttl:        {ttl}s ({ttl / 3600:.1f}h)   period: {period or 'none'}   renewable: {renewable}")
    print(f"  expires:    {data.get('expire_time')}")
    if not renewable:
        sys.exit("  FAIL: token is not renewable — it lapses at the time above and must be re-minted.")
    if period and isinstance(ttl, int) and ttl < period / 2:
        print("  NOTE: less than half the period remains — renew soon.")
else:
    auth = doc.get("auth") or {}
    lease = auth.get("lease_duration") or 0
    print(f"  renewed:    policies={auth.get('policies')} new ttl={lease}s ({lease / 3600:.1f}h)")
PY

if [ "$mode" = "renew" ]; then
  printf '%s\n' "OK — token renewed. It lapses only if nothing renews it again within the period."
else
  printf '%s\n' "OK — token is alive and renewable."
fi
