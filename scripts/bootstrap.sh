#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Olympus bootstrap: turn a fresh clone into a working factory installation.
#
# What it does, in order:
#   1. Loads local .env (gitignored) for OMNIROUTE_* / VAULT_* variables.
#   2. Checks prerequisites (git, curl, node/npm, python3).
#   3. Installs, when missing: the omniroute CLI, the codex CLI, the Claude
#      Code CLI, and the archon CLI (the factory's workflow engine).
#   4. Makes sure an OmniRoute server is reachable at OMNIROUTE_BASE_URL
#      (default http://localhost:20128). If it is not running locally, it
#      tries scripts/omniroute-vault.sh; otherwise it prints how to start
#      it and continues (the agents still get wired, ready for when it is up).
#   5. Ensures an OmniRoute API key exists and is stored where the agents and
#      the omniroute CLI can find it (~/.omniroute/.env, gitignored).
#   6. Wires the coding agents to OmniRoute (Responses API for Codex):
#        codex  -> ~/.codex/config.toml + auth.json + per-model profiles (wire_api=responses)
#        claude -> ~/.claude/settings.json (ANTHROPIC_BASE_URL/AUTH_TOKEN)
#   7. Runs `python3 factory/doctor.py` and prints the result.
#
# Idempotent: safe to re-run; steps already done are skipped.
#
# Environment overrides:
#   OMNIROUTE_BASE_URL        e.g. http://192.168.1.10:20128  (default http://localhost:20128)
#   OMNIROUTE_API_KEY         reuse an existing key instead of creating one
#   OMNIROUTE_ADMIN_PASSWORD  dashboard/admin password used to mint a key
#                             (falls back to the Vault secret INITIAL_PASSWORD)
#   SKIP_AGENT_INSTALL=1      do not install agents, only wire existing ones
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/.."   # repository root

# ---------------------------------------------------------------------------
# 1. Local environment
# ---------------------------------------------------------------------------
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

OMNIROUTE_BASE_URL="${OMNIROUTE_BASE_URL:-http://localhost:20128}"
say()  { printf '\n\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 2. Prerequisites
# ---------------------------------------------------------------------------
say "Checking prerequisites"
missing=""
for tool in git curl python3 node npm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    missing="$missing $tool"
  fi
done
if [ -n "$missing" ]; then
  echo "Missing:$missing" >&2
  echo "Install them first, e.g. on Debian/Ubuntu: sudo apt-get update && sudo apt-get install -y git curl python3 nodejs npm" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Tool installs (idempotent)
# ---------------------------------------------------------------------------
install_omniroute() {
  say "Installing the OmniRoute CLI"
  npm install -g omniroute
}
install_codex() {
  say "Installing the Codex CLI"
  npm install -g @openai/codex
}
install_claude() {
  say "Installing the Claude Code CLI"
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
}
install_archon() {
  say "Installing the Archon CLI (factory workflow engine)"
  curl -fsSL https://archon.diy/install | bash
}

if [ "${SKIP_AGENT_INSTALL:-0}" != "1" ]; then
  command -v omniroute >/dev/null 2>&1 || install_omniroute
  command -v codex     >/dev/null 2>&1 || install_codex
  command -v claude    >/dev/null 2>&1 || install_claude
  command -v archon    >/dev/null 2>&1 || install_archon
else
  say "SKIP_AGENT_INSTALL=1: skipping agent installs, wiring only"
fi

# Re-source PATH in case the claude installer just added ~/.local/bin
export PATH="$HOME/.local/bin:$PATH"

# ---------------------------------------------------------------------------
# 4. OmniRoute server reachability
# ---------------------------------------------------------------------------
say "Checking OmniRoute at ${OMNIROUTE_BASE_URL}"
server_up=0
if curl -fsS -m 5 "${OMNIROUTE_BASE_URL}/healthz" >/dev/null 2>&1; then
  server_up=1
fi

if [ "$server_up" -eq 0 ]; then
  warn "${OMNIROUTE_BASE_URL} is not reachable"
  # The launcher resolves the secret from Vault over curl, so there is no CLI
  # dependency here — only VAULT_ADDR and a token (VAULT_TOKEN or a token file).
  if [ -f scripts/omniroute-vault.sh ] && [ -n "${VAULT_ADDR:-}" ]; then
    say "Starting OmniRoute via scripts/omniroute-vault.sh (Vault-backed)"
    bash scripts/omniroute-vault.sh >/tmp/omniroute-bootstrap.log 2>&1 &
    for _ in $(seq 1 30); do
      if curl -fsS -m 2 "${OMNIROUTE_BASE_URL}/healthz" >/dev/null 2>&1; then
        server_up=1
        break
      fi
      sleep 2
    done
    if [ "$server_up" -eq 0 ]; then
      warn "OmniRoute did not become healthy; see /tmp/omniroute-bootstrap.log"
    fi
  else
    warn "Start OmniRoute manually, e.g. 'bash scripts/omniroute-vault.sh' (needs VAULT_ADDR + a token)"
    warn "The agents below will be wired now and will work as soon as the server is up."
  fi
fi

# ---------------------------------------------------------------------------
# 5. API key
# ---------------------------------------------------------------------------
key="${OMNIROUTE_API_KEY:-}"
if [ -z "$key" ] && [ -f "$HOME/.omniroute/.env" ]; then
  # shellcheck disable=SC1091
  key="$(grep -E '^OMNIROUTE_API_KEY=' "$HOME/.omniroute/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
fi

if [ -z "$key" ] && [ "$server_up" -eq 1 ]; then
  say "No OMNIROUTE_API_KEY set — creating one on ${OMNIROUTE_BASE_URL}"
  admin_password="${OMNIROUTE_ADMIN_PASSWORD:-}"
  # Fall back to the stack's secret in Cerulean Vault (KV v2). This reads over
  # curl so no Vault CLI is needed; absent address or token just means "no
  # fallback" rather than an error.
  vault_token="${VAULT_TOKEN:-}"
  if [ -z "$vault_token" ] && [ -n "${VAULT_TOKEN_FILE:-}" ] && [ -r "${VAULT_TOKEN_FILE}" ]; then
    vault_token="$(tr -d '\r\n' < "${VAULT_TOKEN_FILE}")"
  fi
  if [ -z "$admin_password" ] && [ -n "${VAULT_ADDR:-}" ] && [ -n "$vault_token" ]; then
    admin_password="$(
      curl -sS -m 15 ${VAULT_SKIP_VERIFY:+-k} -H "X-Vault-Token: $vault_token" \
        "${VAULT_ADDR}/v1/${VAULT_PREFIX:-cerulean}/data/${VAULT_PATH:-olympus}" \
        | python3 -c 'import json,sys
print(json.load(sys.stdin).get("data",{}).get("data",{}).get("INITIAL_PASSWORD",""))' 2>/dev/null || true
    )"
  fi
  if [ -n "$admin_password" ]; then
    token="$(
      curl -fsS -m 10 -X POST "${OMNIROUTE_BASE_URL}/api/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"password\":\"${admin_password}\"}" 2>/dev/null \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("accessToken") or d.get("jwt") or "")' 2>/dev/null || true
    )"
    if [ -n "$token" ]; then
      created="$(
        curl -fsS -m 10 -X POST "${OMNIROUTE_BASE_URL}/api/keys" \
          -H 'Content-Type: application/json' \
          -H "Authorization: Bearer ${token}" \
          -d '{"label":"factory-bootstrap"}' 2>/dev/null \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("key") or d.get("apiKey") or d.get("value") or "")' 2>/dev/null || true
      )"
      if [ -n "$created" ]; then
        key="$created"
        mkdir -p "$HOME/.omniroute"
        touch "$HOME/.omniroute/.env"
        if ! grep -q '^OMNIROUTE_API_KEY=' "$HOME/.omniroute/.env" 2>/dev/null; then
          printf '\nOMNIROUTE_API_KEY=%s\n' "$key" >> "$HOME/.omniroute/.env"
        fi
        say "API key created and stored in ~/.omniroute/.env (gitignored)"
      else
        warn "Key creation failed — create one in the dashboard at ${OMNIROUTE_BASE_URL}/login and export OMNIROUTE_API_KEY, then re-run."
      fi
    else
      warn "Dashboard login failed — create a key in the dashboard at ${OMNIROUTE_BASE_URL}/login and export OMNIROUTE_API_KEY, then re-run."
    fi
  else
    warn "No OMNIROUTE_ADMIN_PASSWORD and no Vault INITIAL_PASSWORD — create a key in the dashboard at ${OMNIROUTE_BASE_URL}/login and export OMNIROUTE_API_KEY, then re-run."
  fi
fi

if [ -z "$key" ]; then
  warn "Proceeding without an API key. Agents will be configured with OMNIROUTE_API_KEY from the environment; export it and re-run when you have one."
  key="${OMNIROUTE_API_KEY:-}"
fi

api_key_args=()
[ -n "$key" ] && api_key_args=(--api-key "$key")

# ---------------------------------------------------------------------------
# 6. Wire agents
# ---------------------------------------------------------------------------
if command -v codex >/dev/null 2>&1; then
  say "Wiring Codex to ${OMNIROUTE_BASE_URL}"
  if [ -n "$key" ]; then
    # --api-key sk-... trips on the dash in the key value; use env-var form reliably
    OMNIROUTE_API_KEY="$key" omniroute setup-codex --remote "${OMNIROUTE_BASE_URL}" >/dev/null 2>&1 || \
      warn "omniroute setup-codex failed (server down?). Profiles will be written next run."
  fi

  # Base config: point codex at OmniRoute (Responses API)
  mkdir -p "$HOME/.codex"
  if [ ! -f "$HOME/.codex/config.toml" ] || ! grep -q 'openai_base_url' "$HOME/.codex/config.toml" 2>/dev/null; then
    cat > "$HOME/.codex/config.toml" <<EOF
openai_base_url = "${OMNIROUTE_BASE_URL}/v1"
requires_openai_auth = true
model = "auto/coding"
model_provider = "omniroute"

[model_providers.omniroute]
name = "OmniRoute"
base_url = "${OMNIROUTE_BASE_URL}/v1"
wire_api = "responses"
requires_openai_auth = true
EOF
  else
    # Existing config — ensure Responses API fields are present (idempotent)
    if ! grep -q 'wire_api' "$HOME/.codex/config.toml" 2>/dev/null; then
      python3 - <<PYEOF 2>/dev/null || true
import pathlib
p=pathlib.Path.home()/".codex/config.toml"
t=p.read_text(encoding="utf-8", errors="replace")
if "wire_api" not in t:
    t=t.rstrip()+"\n\n[model_providers.omniroute]\nname = \"OmniRoute\"\nbase_url = \"${OMNIROUTE_BASE_URL}/v1\"\nwire_api = \"responses\"\nrequires_openai_auth = true\n"
    p.write_text(t)
PYEOF
    fi
  fi

  # auth.json holds the API key for codex (update if changed)
  if [ -n "$key" ]; then
    mkdir -p "$HOME/.codex"
    if [ ! -f "$HOME/.codex/auth.json" ] || ! grep -qF "$key" "$HOME/.codex/auth.json" 2>/dev/null; then
      printf '{\n  "auth_mode": "apikey",\n  "OPENAI_API_KEY": "%s"\n}\n' "$key" > "$HOME/.codex/auth.json"
    fi
  fi

  # The harness calls `omniroute launch-codex -p auto-coding`; make sure a
  # profile of that name exists. Prefer one generated by setup-codex.
  if [ ! -f "$HOME/.codex/auto-coding.config.toml" ]; then
    first_profile="$(ls "$HOME/.codex/"*.config.toml 2>/dev/null | grep -v '/config.toml$' | head -1 || true)"
    if [ -n "$first_profile" ]; then
      cp "$first_profile" "$HOME/.codex/auto-coding.config.toml"
      say "Created ~/.codex/auto-coding.config.toml from ${first_profile##*/}"
    else
      cat > "$HOME/.codex/auto-coding.config.toml" <<'EOF'
# codex --profile auto-coding
model                          = "auto/coding"
model_provider                 = "omniroute"
EOF
    fi
  fi
fi

if command -v claude >/dev/null 2>&1; then
  say "Wiring Claude Code to ${OMNIROUTE_BASE_URL}"
  if [ -n "$key" ]; then
    OMNIROUTE_API_KEY="$key" omniroute setup-claude --remote "${OMNIROUTE_BASE_URL}" >/dev/null 2>&1 || \
      warn "omniroute setup-claude failed (server down?). Profiles will be written next run."
  fi

  # settings.json env: point claude at OmniRoute, preserving any existing keys
  mkdir -p "$HOME/.claude"
  python3 - "$key" <<'PYEOF'
import json, os, sys
key = sys.argv[1] or None
path = os.path.expanduser("~/.claude/settings.json")
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}
env = data.setdefault("env", {})
env["ANTHROPIC_BASE_URL"] = os.environ.get("OMNIROUTE_BASE_URL", "http://localhost:20128") + "/v1"
if key:
    env["ANTHROPIC_AUTH_TOKEN"] = key
env["CLAUDE_USE_GLOBAL_AUTH"] = "false"
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PYEOF
fi

# ---------------------------------------------------------------------------
# 7. Verify
# ---------------------------------------------------------------------------
say "Running factory doctor"
python3 factory/doctor.py || true

say "Bootstrap complete"
echo
echo "  OmniRoute:  ${OMNIROUTE_BASE_URL}"
echo "  Codex:      $(command -v codex  >/dev/null 2>&1 && codex --version 2>/dev/null || echo 'not installed')"
echo "  Claude:     $(command -v claude >/dev/null 2>&1 && claude --version 2>/dev/null || echo 'not installed')"
echo "  Archon:     $(command -v archon >/dev/null 2>&1 && archon --version 2>&1 | head -1 || echo 'not installed')"
echo
echo "Next steps:"
echo "  - Watch one lap:        archon workflow run factory-implement --branch factory/impl-1 \"implement gh:issue:<n>\""
echo "  - Audit readiness:      python3 factory/doctor.py"
echo "  - Start OmniRoute:      bash scripts/omniroute-vault.sh   (needs VAULT_ADDR + a token)"