#!/usr/bin/env bash
# stack-lib.sh — the central shared library for Innotel Platform Stack tasks.
#
# Every platform repo's setup/npm scripts can source this from the canonical
# stack repo (`ips`) so common tasks live in ONE place:
#
#   STACK_LIB="${STACK_LIB:-/usr/local/lib/innotel/stack-lib.sh}"
#   [ -f "$STACK_LIB" ] && . "$STACK_LIB"
#
# Or mirror a copy into the repo (see ./scripts/sync-stack-lib.sh) and source
# it relative to the repo root:
#
#   . "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/stack-lib.sh"
#
# The library is deliberately POSIX-ish (bash) and dependency-free: stdlib
# curl/jq/dig only — the same posture as every platform's npm provisioner.

# ── env helpers ─────────────────────────────────────────────────────────────
# Resolve a setting: real env first, then a repo .env, then a default.
stack_lib_env() { # key [default]
  local key="$1" default="${2:-}"
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return 0; fi
  local f
  for f in .env .env.example; do
    [ -f "$f" ] || continue
    local v
    v="$(sed -n "s/^${key}=//p" "$f" | head -1 | tr -d '\r')"
    if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
  done
  printf '%s' "$default"
}

# Load KEY=VALUE lines from a .env into the current shell (no override).
stack_lib_load_env() { # [path]
  local f="${1:-.env}"
  [ -f "$f" ] || return 0
  local line key val
  while IFS= read -r line; do
    line="${line%%$'\r'*}"
    case "$line" in ''|'#'*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    [ -z "${!key:-}" ] && export "$key=$val"
  done < "$f"
}

# ── network helpers ─────────────────────────────────────────────────────────
# The host's primary LAN IPv4 (the address NPM must forward to). Prefers the
# default-route interface; never a docker bridge (172.x) or loopback.
stack_lib_lan_ip() { # -> prints the LAN IP or empty
  local ip
  ip="$(ip -4 route get 1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -1)"
  if [ -n "$ip" ] && [ "$ip" != "127.0.0.1" ]; then printf '%s' "$ip"; return 0; fi
  ip="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^192\.168\.|^10\.|^172\.(1[6-9]|2[0-9]|3[01])\.' | grep -v '^172\.' | head -1)"
  [ -n "$ip" ] && printf '%s' "$ip"
}

# Resolve the forward host for NPM: explicit NPM_FORWARD_HOST wins, else the
# LAN IP, else `host.docker.internal` (the docker-internal alias).
stack_lib_forward_host() { # [explicit] <override> -> prints the forward host (pass "$@")
  local explicit="${1:-}"
  if [ -n "$explicit" ]; then printf '%s' "$explicit"; return 0; fi
  local lan
  lan="$(stack_lib_lan_ip)"
  if [ -n "$lan" ]; then printf '%s' "$lan"; return 0; fi
  printf 'host.docker.internal'
}

# ── NPM API helpers (stdlib curl) ───────────────────────────────────────────
stack_lib_npm_login() { # api_url email password -> prints token or empty
  local api="$1" email="$2" password="$3"
  curl -sf -X POST "$api/api/tokens" -H 'Content-Type: application/json' \
    -d "{\"identity\":\"$email\",\"secret\":\"$password\"}" \
    | sed -n 's/.*"token":"\([^"]*\)".*/\1/p'
}

stack_lib_npm_get() { # api_url token path -> prints JSON body (or empty)
  local api="$1" token="$2" path="$3"
  curl -sf "$api$path" -H "Authorization: Bearer $token"
}

stack_lib_npm_post() { # api_url token path json-body -> prints JSON body
  local api="$1" token="$2" path="$3" body="$4"
  curl -sf -X POST "$api$path" -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' -d "$body"
}

stack_lib_npm_put() { # api_url token path json-body -> prints JSON body
  local api="$1" token="$2" path="$3" body="$4"
  curl -sf -X PUT "$api$path" -H "Authorization: Bearer $token" \
    -H 'Content-Type: application/json' -d "$body"
}

# ── common output helpers ───────────────────────────────────────────────────
stack_lib_say()  { printf '\033[1m%s\033[0m\n' "$*"; }
stack_lib_warn() { printf '\033[33mwarning: %s\033[0m\n' "$*" >&2; }
stack_lib_die()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# ── self-test (./scripts/stack-lib.sh --selftest) ───────────────────────────
if [ "${1:-}" = "--selftest" ]; then
  echo "lan_ip=$(stack_lib_lan_ip)"
  echo "forward_host=$(stack_lib_forward_host "$1")"
  echo "env_test=$(stack_lib_env NPM_BASE_DOMAIN '(unset)')"
fi