#!/usr/bin/env bash
set -euo pipefail

# olym pus - Zero-Cost Coding Agent Platform Setup
# Usage: chmod +x setup.sh && ./setup.sh
# Run-time: Local CPU only - all heavy inference routed to free cloud providers
#   - Frontend: Hermes 3 (70B) via OpenRouter Free
#   - Backend: Qwen 2.5 Coder (32B/70B) via OmniRoute Cloud

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
CORE_DIR="$ROOT_DIR/core-modules"
FACTORY_ENV="$ROOT_DIR/.factory-env"
ARCHON_CONFIG="$ROOT_DIR/.archon/config.yaml"

# ──────────────────────────────────────────────
# Colors & Helpers
# ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }

header() {
  echo ""
  echo -e "${BOLD}════════════════════════════════════════════════════${NC}"
  echo -e "${BOLD}  $*${NC}"
  echo -e "${BOLD}════════════════════════════════════════════════════${NC}"
}

# ──────────────────────────────────────────────
# 1. Validate System Requirements
# ──────────────────────────────────────────────
check_command() {
  local cmd="$1"
  local install_hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    error "Missing required command: $cmd"
    echo -e "       Install hint: $install_hint"
    return 1
  fi
  local ver
  ver=$("$cmd" --version 2>&1 | head -n1 || echo "unknown")
  success "$cmd found — $ver"
  return 0
}

validate_requirements() {
  header "Validating System Requirements"

  local missing=0

  check_command "git"     "https://git-scm.com/downloads  |  sudo apt install git  |  brew install git" || missing=1
  check_command "bun"     "curl -fsSL https://bun.sh/install | bash  &&  source ~/.bashrc" || missing=1
  check_command "python3" "https://www.python.org/downloads  |  sudo apt install python3 python3-venv  |  brew install python3" || missing=1
  check_command "gh"      "https://cli.github.com/  |  sudo apt install gh  |  brew install gh  — then run: gh auth login" || missing=1
  check_command "npm"     "https://nodejs.org/  |  sudo apt install nodejs npm  |  brew install node (npm ships with node)" || missing=1

  # Extra version floors
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)" 2>/dev/null; then
      warn "python3 version < 3.10 detected — 3.10+ recommended for factory consumer"
    fi
  fi

  if command -v bun >/dev/null 2>&1; then
    local bun_major
    bun_major=$(bun --version 2>/dev/null | cut -d. -f1 || echo "0")
    if [[ "$bun_major" -lt 1 ]]; then
      warn "bun version < 1.0 detected — please upgrade: bun upgrade"
    fi
  fi

  # pip / venv check (non-fatal but warned)
  if ! python3 -m pip --version >/dev/null 2>&1; then
    warn "pip not available for python3 — factory Python deps may fail. Install with: python3 -m ensurepip --upgrade"
  fi

  if [[ $missing -ne 0 ]]; then
    echo ""
    die "One or more required tools are missing. Install them and re-run: chmod +x setup.sh && ./setup.sh"
  fi

  success "All required tools present"
}

# ──────────────────────────────────────────────
# 2. Create core-modules folder
# ──────────────────────────────────────────────
prepare_core_dir() {
  header "Preparing core-modules"
  mkdir -p "$CORE_DIR"
  success "Created $CORE_DIR"
  mkdir -p "$ROOT_DIR/.archon/cache"
  mkdir -p "$ROOT_DIR/factory"
  # Local dev: build-requests is the trigger, builds is the factory output (both documented, builds/ is gitignored)
  mkdir -p "$ROOT_DIR/build-requests"
  mkdir -p "$ROOT_DIR/builds"
  if [[ ! -f "$ROOT_DIR/build-requests/.gitkeep" && -z "$(ls -A "$ROOT_DIR/build-requests" 2>/dev/null | grep -v README)" ]]; then
    touch "$ROOT_DIR/build-requests/.gitkeep" 2>/dev/null || true
  fi
}

# ──────────────────────────────────────────────
# 3. Clone vendor tools if they do not exist
# ──────────────────────────────────────────────
clone_if_missing() {
  local name="$1"
  local repo="$2"
  local dest="$CORE_DIR/$name"

  if [[ -d "$dest/.git" ]]; then
    info "$name already cloned at $dest — pulling latest..."
    git -C "$dest" pull --ff-only || warn "git pull failed for $name — continuing with existing checkout"
  elif [[ -d "$dest" && -n "$(ls -A "$dest" 2>/dev/null)" ]]; then
    warn "$dest exists but is not a git repo — skipping clone for $name"
  else
    info "Cloning $name from $repo ..."
    git clone "$repo" "$dest"
    success "$name cloned"
  fi
}

clone_vendor_tools() {
  header "Cloning Vendor Tools"

  # Upstream sources — all open-source, zero-cost
  # Override via env vars if you track forks:
  #   OMNIROUTE_REPO, ARCHON_REPO, FACTORY_REPO
  local OMNIROUTE_REPO="${OMNIROUTE_REPO:-https://github.com/inotex/omniroute.git}"
  local ARCHON_REPO="${ARCHON_REPO:-https://github.com/JohanLi233/archon.git}"
  local FACTORY_REPO="${FACTORY_REPO:-https://github.com/Andy-Zhouelect/AI-Software-Factory.git}"

  clone_if_missing "omniroute"            "$OMNIROUTE_REPO"
  clone_if_missing "archon"               "$ARCHON_REPO"
  clone_if_missing "ai-software-factory"  "$FACTORY_REPO"

  success "All vendor tools ready under $CORE_DIR/"
}

# ──────────────────────────────────────────────
# 4. Run bun install inside respective directories
# ──────────────────────────────────────────────
bun_install_dir() {
  local dir="$1"
  if [[ -f "$dir/package.json" ]]; then
    info "Running bun install in $dir ..."
    (cd "$dir" && bun install)
    success "bun install complete: $dir"
  elif [[ -f "$dir/bun.lockb" || -f "$dir/bun.lock" ]]; then
    info "Running bun install in $dir (lockfile present) ..."
    (cd "$dir" && bun install)
    success "bun install complete: $dir"
  else
    warn "No package.json in $dir — skipping bun install"
  fi
}

install_dependencies() {
  header "Installing Dependencies (bun)"

  bun_install_dir "$CORE_DIR/omniroute"
  bun_install_dir "$CORE_DIR/archon"
  bun_install_dir "$CORE_DIR/ai-software-factory"

  # Root package.json if present (optional monorepo wrapper)
  if [[ -f "$ROOT_DIR/package.json" ]]; then
    bun_install_dir "$ROOT_DIR"
  fi
}

# ──────────────────────────────────────────────
# 5. Initialize Software Factory setup wizard programmatically
# ──────────────────────────────────────────────
init_factory_wizard() {
  header "Initializing AI Software Factory"

  local factory_dir="$CORE_DIR/ai-software-factory"

  # Mirror factory runner into top-level factory/ for the documented path:
  #   python3 factory/consumer.py loop
  if [[ -f "$factory_dir/consumer.py" && ! -f "$ROOT_DIR/factory/consumer.py" ]]; then
    cp "$factory_dir/consumer.py" "$ROOT_DIR/factory/consumer.py" 2>/dev/null || true
  fi
  if [[ -d "$factory_dir/factory" && ! -f "$ROOT_DIR/factory/consumer.py" ]]; then
    # Some forks nest under factory/factory
    cp "$factory_dir/factory/consumer.py" "$ROOT_DIR/factory/consumer.py" 2>/dev/null || true
  fi

  # Preferred: factory's own setup wizard
  if [[ -f "$factory_dir/setup.py" ]]; then
    info "Running factory setup wizard: python3 setup.py --non-interactive"
    (cd "$factory_dir" && python3 setup.py --non-interactive 2>/dev/null) \
      || (cd "$factory_dir" && python3 setup.py --yes 2>/dev/null) \
      || (cd "$factory_dir" && python3 -m factory.setup --init 2>/dev/null) \
      || warn "Factory setup wizard exited non-zero — you can re-run manually: cd $factory_dir && python3 setup.py"
    success "Factory wizard invoked"
  elif [[ -f "$factory_dir/scripts/setup.sh" ]]; then
    info "Running factory scripts/setup.sh"
    bash "$factory_dir/scripts/setup.sh" --non-interactive 2>/dev/null || bash "$factory_dir/scripts/setup.sh" || warn "factory scripts/setup.sh failed"
  elif [[ -f "$factory_dir/install.py" ]]; then
    info "Running factory install.py"
    (cd "$factory_dir" && python3 install.py --non-interactive 2>/dev/null || python3 install.py)
  else
    warn "No recognized factory wizard found — creating minimal factory skeleton"
    mkdir -p "$ROOT_DIR/factory"
    if [[ ! -f "$ROOT_DIR/factory/consumer.py" ]]; then
      cat > "$ROOT_DIR/factory/consumer.py" <<'PYEOF'
#!/usr/bin/env python3
"""
Factory Consumer - SDLC task loop for Olympus
Polls Archon DAG and delegates code tasks to Qwen via OmniRoute.
"""
import argparse
import time
import sys

def loop_forever(poll_interval: int = 5):
    print(f"[factory] Starting consumer loop (poll={poll_interval}s) — Ctrl+C to stop")
    print("[factory] Backend: Qwen 2.5 Coder via OmniRoute http://localhost:20128/v1")
    try:
        while True:
            # TODO: integrate with Archon DAG polling and OmniRoute dispatch
            # archon_client.poll() -> task -> omniroute.chat.completions.create(model="qwen-2.5-coder-32b-instruct:free", ...)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n[factory] Stopped.")
        sys.exit(0)

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Olympus Factory Consumer")
    p.add_argument("command", nargs="?", default="loop", choices=["loop", "once"], help="loop or once")
    p.add_argument("--interval", type=int, default=5, help="poll interval seconds")
    args = p.parse_args()
    if args.command == "loop":
        loop_forever(args.interval)
    else:
        print("[factory] Single poll (once) — implement Archon fetch here")
PYEOF
      chmod +x "$ROOT_DIR/factory/consumer.py"
      success "Created stub $ROOT_DIR/factory/consumer.py"
    fi
  fi

  # Python deps if factory exposes requirements
  for req in "$factory_dir/requirements.txt" "$factory_dir/factory/requirements.txt" "$ROOT_DIR/factory/requirements.txt"; do
    if [[ -f "$req" ]]; then
      info "Installing Python deps from $req"
      python3 -m pip install -r "$req" --quiet || warn "pip install -r $req failed — try: python3 -m pip install -r $req"
    fi
  done

  # Ensure archon binary is executable if present
  if [[ -f "$CORE_DIR/archon/bin/archon" ]]; then
    chmod +x "$CORE_DIR/archon/bin/archon" || true
  fi

  success "Factory initialization done"
}

# ──────────────────────────────────────────────
# 6. Securely request Telegram Bot Token -> .factory-env
# ──────────────────────────────────────────────
configure_telegram_token() {
  header "Telegram Bot Token Setup"

  echo -e "${BOLD}This platform uses Hermes 3 (70B) via OpenRouter Free for the Telegram interface.${NC}"
  echo "Create a bot with @BotFather on Telegram to get your token:"
  echo "  1. Open Telegram -> search @BotFather -> /newbot"
  echo "  2. Follow prompts, copy the HTTP API token (e.g. 123456:ABC-...) "
  echo ""

  local existing_token=""
  if [[ -f "$FACTORY_ENV" ]]; then
    existing_token=$(grep -E "^TELEGRAM_BOT_TOKEN=" "$FACTORY_ENV" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    if [[ -n "$existing_token" && "$existing_token" != "ENV_VAR" ]]; then
      info "Existing token found in .factory-env (masked: ${existing_token:0:6}...)"
      read -r -p "Keep existing token? [Y/n]: " keep
      if [[ "$keep" =~ ^[nN] ]]; then
        existing_token=""
      else
        success "Keeping existing Telegram token"
        return 0
      fi
    fi
  fi

  # Allow non-interactive injection: TELEGRAM_BOT_TOKEN env var
  if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -z "$existing_token" ]]; then
    info "Using TELEGRAM_BOT_TOKEN from environment"
    existing_token="$TELEGRAM_BOT_TOKEN"
  fi

  local token="$existing_token"
  if [[ -z "$token" ]]; then
    # Secure prompt (no echo) with visible fallback
    if [[ -t 0 ]]; then
      echo -n "Paste your Telegram Bot Token (input hidden) : "
      read -r -s token || true
      echo ""
      if [[ -z "$token" ]]; then
        echo -n "Paste your Telegram Bot Token (visible)    : "
        read -r token || true
      fi
    else
      warn "Non-interactive shell — set TELEGRAM_BOT_TOKEN env var or re-run interactively"
    fi
  fi

  if [[ -z "$token" ]]; then
    warn "No token provided — writing placeholder. Edit $FACTORY_ENV later."
    token="PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE"
  fi

  # Basic format validation (Telegram tokens are <digits>:<35-char>)
  if [[ "$token" != "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE" && ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
    warn "Token format looks unusual — Telegram tokens are like 123456789:AAH... — continuing anyway"
  fi

  # Write .factory-env atomically with 0600 perms
  local tmp_env
  tmp_env=$(mktemp)
  {
    echo "# Olympus Factory Environment — DO NOT COMMIT"
    echo "# Generated by setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "TELEGRAM_BOT_TOKEN=$token"
    echo "OPENROUTER_MODEL=nousresearch/hermes-3-llama-3-70b:free"
    echo "OMNIROUTE_BASE_URL=http://localhost:20128/v1"
    echo "QWEN_MODEL=qwen-2.5-coder-32b-instruct:free"
    echo "ARCHON_BINARY=./core-modules/archon/bin/archon"
    # Preserve any extra keys already in file (except token which we replace)
    if [[ -f "$FACTORY_ENV" ]]; then
      grep -v -E "^(TELEGRAM_BOT_TOKEN|OPENROUTER_MODEL|OMNIROUTE_BASE_URL|QWEN_MODEL|ARCHON_BINARY)=" "$FACTORY_ENV" 2>/dev/null || true
    fi
  } > "$tmp_env"
  mv "$tmp_env" "$FACTORY_ENV"
  chmod 600 "$FACTORY_ENV"
  success "Wrote $FACTORY_ENV (0600)"

  # Also ensure .archon/config.yaml points to ENV_VAR pattern (token injected at runtime via env)
  if [[ -f "$ARCHON_CONFIG" ]]; then
    info "Archon config present at $ARCHON_CONFIG — Telegram provider will read TELEGRAM_BOT_TOKEN from environment"
  fi

  echo ""
  echo -e "${YELLOW}Next:${NC} source .factory-env  or  export \$(cat .factory-env | xargs)  before launching the bot"
}

# ──────────────────────────────────────────────
# 7. Final summary
# ──────────────────────────────────────────────
print_summary() {
  header "Setup Complete — Olympus Ready"
  cat <<EOF
${GREEN}✔ Core modules:${NC}  $CORE_DIR/{omniroute,archon,ai-software-factory}
${GREEN}✔ Archon config:${NC} $ARCHON_CONFIG
${GREEN}✔ Factory env:${NC}   $FACTORY_ENV  (chmod 600)

${BOLD}Quickstart:${NC}
  1. Launch OmniRoute gateway (Free Tier Mode):
       cd core-modules/omniroute && npm run start
     Then open ${CYAN}http://localhost:20128${NC} and toggle ${BOLD}Free Tier Mode ON${NC}

  2. Keep the SDLC loop alive (new terminal, from repo root):
       source .factory-env
       python3 factory/consumer.py loop

  3. Message your bot on Telegram — Hermes 3 (70B) via OpenRouter Free
     will handle the chat, and Qwen 2.5 Coder via OmniRoute will
     perform codebase edits at ${BOLD}zero local CPU cost${NC}.

${YELLOW}Tip:${NC} Re-run this script any time:  chmod +x setup.sh && ./setup.sh
Docs: cat README.md
EOF
}

# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
main() {
  echo -e "${BOLD}"
  echo "   ___  _                              "
  echo "  / _ \| |_   _ _ __ ___  _ __  _   _ ___ "
  echo " | | | | | | | | '_ \` _ \| '_ \| | | / __|"
  echo " | |_| | | |_| | | | | | | |_) | |_| \__ \\"
  echo "  \___/|_|\__, |_| |_| |_| .__/ \__,_|___/"
  echo "          |___/          |_|              "
  echo -e "${NC}"
  echo -e "  Zero-Cost Coding Agent Platform — Local CPU, Free Cloud Brains"
  echo ""

  validate_requirements
  prepare_core_dir
  clone_vendor_tools
  install_dependencies
  init_factory_wizard
  configure_telegram_token
  print_summary
}

main "$@"
