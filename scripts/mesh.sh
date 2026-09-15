#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# mesh.sh — Innotel Platform Stack mesh control plane
# ══════════════════════════════════════════════════════════════════════════════
# One verb per thing you do to a machine in the mesh. The Mesh is the
# WireGuard overlay (10.10.0.0/16) + Consul registry that lets the group
# servers talk to each other, and the group dirs that say which repos run on
# which server.
#
#   join      enroll this host in the WireGuard + Consul mesh
#   leave     drain this host out of the mesh
#   download  fetch member repos into their group dirs
#   install   lay out the workspace, download, then join and/or start
#   deploy    do the same to a REMOTE host over SSH, from here: pick the
#             components (roster repos + stack extensions) interactively, then
#             lay out the workspace, clone them, fill each one's .env with
#             generated credentials (printed once, optionally pushed to Vault)
#             and, with --join/--up, enrol or start them there
#
# Plus the two read-only conveniences:
#
#   status    what this host is, what it runs, whether the mesh is up
#   discover  find a service that some node registered in Consul
#
# ── Layout ────────────────────────────────────────────────────────────────────
# The mesh is a workspace, not a repo. Every member repo is checked out inside
# the group dir for the server that runs it, and the orchestrator sits beside
# them:
#
#   <root>/
#     1-primary/   cerulean atheniq magnate signara sign verifier npm
#     2-voice/     capstone zeus
#     3-media/     monarch plutus
#     4-social/    rizzaura onyx zapit
#     5-dev/       atlas distro oasis olympus
#     ips/         the platform stack (stack.sh, mesh/, docs/)
#
# A repo may live anywhere — mesh.sh figures out its server from its own path
# when it is inside a group dir, and takes --server/--group when it is not.
#
# ── Usage ─────────────────────────────────────────────────────────────────────
#   scripts/mesh.sh join   [--server N] [--hub-pubkey KEY] [--stack DIR]
#                          [--no-verify] [--dry-run]
#   scripts/mesh.sh leave  [--purge] [--dry-run]
#   scripts/mesh.sh download [repo...] [--group N] [--all] [--pull]
#   scripts/mesh.sh install  [repo...] [--group N] [--all] [--join] [--up]
#                            [--pull] [--dry-run]
#   scripts/mesh.sh deploy   --host [user@]host [--ssh-port N] [--ssh-key FILE]
#                            [--components LIST] [--root DIR] [--join] [--up]
#                            [--vault] [--vault-path PATH] [--credentials-file F]
#                            [--dry-run]
#                            LIST = all | repos | exts | 1-5 | a group name |
#                                   comma/space separated repo or ext names
#                            (no LIST → interactive multi-select)
#   scripts/mesh.sh status | discover <service> | help
#
# Environment overrides
#   MESH_GIT_BASE   git host for download      (https://github.com/innotelinc)
#   MESH_STACK_DIR  the platform-stack checkout (auto: <root>/ips)
#   MESH_DEV_ROOT   the workspace root          (auto: parent of a group dir)
#   MESH_DEPLOY_ROOT  deploy's target workspace root (default: /opt/innotel)
#   VAULT_ADDR / VAULT_TOKEN / VAULT_TOKEN_FILE  where --vault pushes secrets
#   VAULT_PREFIX    KV v2 mount used by --vault  (default: cerulean)
#
# Canonical copy: ips/scripts/mesh.sh in the platform-stack repo. Mirrored
# verbatim into every member repo by scripts/sync-mesh.sh — edit it there.
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Where is this copy running from? ─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_NAME="$(basename "${REPO_DIR}")"
GROUP_DIR="$(basename "$(dirname "${REPO_DIR}")")"

# Root of the workspace: <root>/<N>-<group>/<repo>  →  two levels up.
DEV_ROOT_DEFAULT=""
if [[ "${GROUP_DIR}" =~ ^[1-5]- ]]; then
  # .../<N>-<group>/<repo>/scripts
  DEV_ROOT_DEFAULT="$(cd "$(dirname "${REPO_DIR}")/.." && pwd)"
elif [ -d "$(dirname "${REPO_DIR}")/1-primary" ] \
  || [ -d "$(dirname "${REPO_DIR}")/2-voice" ]; then
  # .../<root>/ips/scripts — the orchestrator's own copy.
  DEV_ROOT_DEFAULT="$(cd "$(dirname "${REPO_DIR}")" && pwd)"
fi
DEV_ROOT="${MESH_DEV_ROOT:-${DEV_ROOT_DEFAULT}}"

SERVER_N=""
GROUP_NUM=""
HUB_PUBKEY=""
NO_VERIFY=0
DRY_RUN=0
PURGE=0
DO_PULL=0
DO_JOIN=0
DO_UP=0
SELECT_ALL=0
SELECT_GROUP=""
SELECT_REPOS=()

# ── deploy (remote) ──────────────────────────────────────────────────────────
DEPLOY_HOST=""
DEPLOY_ROOT="${MESH_DEPLOY_ROOT:-/opt/innotel}"
SSH_PORT="22"
SSH_KEY=""
COMPONENTS=""
DO_VAULT=0
VAULT_PATH_OVERRIDE=""
CRED_FILE=""
SELECT_COMPONENTS=()
CRED_NAMES=()
CRED_VALUES=()

# ── The roster — which repos live on which server ─────────────────────────────
# <num>|<group>|<repo> [<repo> ...]
read -r -d '' MESH_ROSTER <<'EOF' || true
1|primary|cerulean atheniq magnate signara sign verifier npm
2|voice|capstone zeus
3|media|monarch plutus
4|social|rizzaura onyx zapit
5|dev|atlas distro oasis olympus
EOF

# Repos whose git remote name differs from the dir name it is checked out as.
repo_slug() {
  case "$1" in
    ips)  echo "innotel-platform-stack" ;;
    sign) echo "sign-platform" ;;
    *)    echo "$1" ;;
  esac
}

group_name_for() {  # 2 -> voice   (env-var form is upper-cased on use)
  case "$1" in
    1) echo "primary" ;; 2) echo "voice" ;; 3) echo "media" ;;
    4) echo "social"  ;; 5) echo "dev"   ;;
  esac
}

group_env_for() {   # 2 -> VOICE
  group_name_for "$1" | tr '[:lower:]' '[:upper:]'
}

group_repos() {     # 2 -> "capstone zeus"
  echo "${MESH_ROSTER}" | awk -F'|' -v n="$1" '$1 == n { print $3 }'
}

group_of_repo() {   # capstone -> 2
  echo "${MESH_ROSTER}" | awk -F'|' -v r="$1" '$3 ~ ("(^| )" r "( |$)") { print $1; exit }'
}

all_repos() {
  echo "${MESH_ROSTER}" | awk -F'|' '{ print $3 }' | tr '\n' ' '
}

# ── Output ───────────────────────────────────────────────────────────────────
BLUE='\033[1;34m'; GREEN='\033[1;32m'; YELLOW='\033[1;33m'; RED='\033[1;31m'
BOLD='\033[1m'; NC='\033[0m'
info() { printf "${BLUE}[mesh]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}[mesh]${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}[mesh]${NC} %s\n" "$*" >&2; }
err()  { printf "${RED}[mesh]${NC} %s\n" "$*" >&2; }
die()  { err "$*"; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# ── Locate the platform stack ────────────────────────────────────────────────
find_stack() {
  if [ -n "${MESH_STACK_DIR:-}" ]; then
    STACK_DIR="${MESH_STACK_DIR}"
  elif [ -n "${DEV_ROOT}" ] && [ -d "${DEV_ROOT}/ips" ]; then
    STACK_DIR="${DEV_ROOT}/ips"
  elif [ -d "${REPO_DIR}/../ips" ]; then
    STACK_DIR="$(cd "${REPO_DIR}/../ips" && pwd)"
  elif [ -f "${REPO_DIR}/stack.sh" ]; then
    STACK_DIR="${REPO_DIR}"          # running from inside the stack repo
  else
    STACK_DIR=""
  fi
}

require_stack() {
  find_stack
  [ -n "${STACK_DIR}" ] && [ -f "${STACK_DIR}/stack.sh" ] \
    || die "Platform stack (ips) not found. Pass MESH_STACK_DIR=/path/to/ips."
}

ENV_FILE() { echo "${STACK_DIR:-}/.env"; }

env_upsert() { # idempotent KEY=VALUE write into an env file
  local file="$1" key="$2" value="$3"
  [ -f "$file" ] || : > "$file"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

env_get() { # first match of KEY=VALUE in the stack .env
  local file; file="$(ENV_FILE)"
  [ -f "$file" ] || return 0
  sed -n "s/^$1=//p" "$file" | head -1 | sed 's/[[:space:]]*#.*$//' || true
}

# ── Resolve which server this is ─────────────────────────────────────────────
detect_server() {
  # 1. From our own path: <root>/2-voice/capstone/scripts/mesh.sh
  if [[ "${GROUP_DIR}" =~ ^[1-5]- ]]; then
    echo "${GROUP_DIR%%-*}"; return
  fi
  # 2. From the repo we live in.
  local n; n="$(group_of_repo "${REPO_NAME}")"
  if [ -n "$n" ]; then echo "$n"; return; fi
  # 3. From the hostname.
  local host; host="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  case "$host" in
    *onyx*|*rizz*|*zapit*)                                echo 4; return ;;
    *distro*|*atlas*|*oasis*|*olympus*)                   echo 5; return ;;
    *cerulean*|*magnate*|*atheniq*|*sign*|*npm*)          echo 1; return ;;
    *capstone*|*zeus*)                                    echo 2; return ;;
    *monarch*|*jellyfin*|*plutus*)                        echo 3; return ;;
  esac
  echo ""
}

resolve_server() {
  if [ -n "${GROUP_NUM}" ]; then SERVER_N="${GROUP_NUM}"; fi
  if [ -z "${SERVER_N}" ]; then SERVER_N="$(detect_server)"; fi
  [ -n "${SERVER_N}" ] || die "Cannot tell which server this is. Pass --server N (1-5)."
  case "${SERVER_N}" in
    1|2|3|4|5) ;;
    *) die "Invalid server '${SERVER_N}' (expected 1-5)." ;;
  esac
}

# ── 1. join ──────────────────────────────────────────────────────────────────
gen_keypair() {
  local priv pub
  if have wg; then
    priv="$(wg genkey)"
    pub="$(printf '%s' "$priv" | wg pubkey)"
  elif docker exec mesh-wireguard wg --version >/dev/null 2>&1; then
    priv="$(docker exec mesh-wireguard wg genkey)"
    pub="$(docker exec mesh-wireguard sh -c "printf '%s' '$priv' | wg pubkey")"
  else
    priv="$(timeout 90 docker run --rm linuxserver/wireguard:latest sh -c 'wg genkey' 2>/dev/null || true)"
    [ -n "$priv" ] || die "Cannot generate WireGuard keys (no 'wg', no mesh container, docker pull failed)."
    pub="$(docker run --rm -e PRIV="$priv" linuxserver/wireguard:latest sh -c 'printf %s "$PRIV" | wg pubkey')"
  fi
  printf '%s %s\n' "$priv" "$pub"
}

cmd_join() {
  require_stack
  have docker || die "docker is required to join the mesh."
  resolve_server

  local gname genv mesh_ip consul_addr consul_flag file
  gname="$(group_name_for "${SERVER_N}")"
  genv="$(group_env_for "${SERVER_N}")"
  mesh_ip="10.10.${SERVER_N}.1"
  if [ "${SERVER_N}" = "1" ]; then
    consul_addr="${mesh_ip}";  consul_flag="-server=true -bootstrap-expect=1"
  else
    consul_addr="10.10.1.1";  consul_flag="-server=false"
  fi
  file="$(ENV_FILE)"

  info "Server ${SERVER_N} (group ${gname}) → mesh IP ${mesh_ip}"

  # ── stack .env ─────────────────────────────────────────────────────────────
  if [ ! -f "${file}" ]; then
    if [ -f "${STACK_DIR}/.env.example" ]; then
      cp "${STACK_DIR}/.env.example" "${file}"
      ok "Created ${file} from .env.example"
    else
      die "No ${file} and no .env.example in ${STACK_DIR}."
    fi
  fi

  if [ "${DRY_RUN}" = "1" ]; then
    warn "DRY-RUN — would write the mesh section into ${file} and start the mesh."
    return 0
  fi

  env_upsert "${file}" MESH_NETWORK "10.10.0.0/16"
  env_upsert "${file}" MESH_PORT "${MESH_PORT_OVERRIDE:-51820}"
  for n in 1 2 3 4 5; do
    env_upsert "${file}" "SERVER_${n}_$(group_env_for "$n")_IP" "10.10.${n}.1"
    # Fill a blank public IP only; never clobber a value the operator set.
    if [ -z "$(env_get "SERVER_${n}_PUBLIC_IP")" ]; then
      if [ "${n}" = "${SERVER_N}" ]; then
        local detected
        detected="$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1 || true)"
        if [ -n "${detected}" ]; then
          env_upsert "${file}" "SERVER_${n}_PUBLIC_IP" "${detected}"
          ok "Set SERVER_${n}_PUBLIC_IP=${detected} (auto-detected)"
        else
          warn "Could not auto-detect this host's public IP — set SERVER_${n}_PUBLIC_IP in ${file}."
        fi
      else
        env_upsert "${file}" "SERVER_${n}_PUBLIC_IP" ""
      fi
    fi
  done

  # Consul gossip key — required for cross-node gossip.
  if [ -z "$(env_get REGISTRY_ENCRYPT_KEY)" ]; then
    local key
    key="$(docker run --rm hashicorp/consul:1.19 keygen 2>/dev/null || true)"
    if [ -n "${key}" ]; then
      env_upsert "${file}" REGISTRY_ENCRYPT_KEY "${key}"
      ok "Generated the Consul gossip key"
    else
      warn "Could not generate REGISTRY_ENCRYPT_KEY — set it manually: docker run --rm hashicorp/consul:1.19 keygen"
    fi
  fi
  env_upsert "${file}" CONSUL_SERVER_FLAG "${consul_flag}"
  env_upsert "${file}" CONSUL_SERVER_ADDR "${consul_addr}"
  env_upsert "${file}" REGISTRY_ADDR "10.10.1.1:8500"
  # Consul's own network is the leader's; stack.sh exports these per group and
  # the mesh compose reads them, so pin them here too for a bare `mesh.sh join`.
  export CONSUL_SERVER_ADDR="${consul_addr}"
  export CONSUL_SERVER_FLAG="${consul_flag}"
  export MESH_SUBNET="$(echo "${mesh_ip}" | sed 's/\.[0-9]*$/.0/')"
  export INTERNAL_SUBNET="${MESH_SUBNET}"
  export SERVER_PUBLIC_IP="$(env_get "SERVER_${SERVER_N}_PUBLIC_IP")"
  export REGISTRY_ADDR="10.10.1.1:8500"
  export MESH_PORT="${MESH_PORT_OVERRIDE:-51820}"
  export MESH_NETWORK="10.10.0.0/16"

  # ── this host's WireGuard keypair ──────────────────────────────────────────
  local priv pub
  priv="$(env_get "SERVER_${SERVER_N}_WG_PRIVATE_KEY")"
  pub="$(env_get "SERVER_${SERVER_N}_WG_PUBLIC_KEY")"
  if [ -z "${priv}" ] || [ -z "${pub}" ]; then
    info "Generating the WireGuard keypair for server ${SERVER_N}..."
    local kp; kp="$(gen_keypair)"
    priv="${kp%% *}"; pub="${kp##* }"
    env_upsert "${file}" "SERVER_${SERVER_N}_WG_PRIVATE_KEY" "${priv}"
    env_upsert "${file}" "SERVER_${SERVER_N}_WG_PUBLIC_KEY" "${pub}"
    ok "Keypair generated — public key: ${pub}"
  else
    ok "Using the existing keypair (pub ${pub})"
  fi

  # ── hub vs client wiring ───────────────────────────────────────────────────
  local wg_data client_compose
  wg_data="${STACK_DIR}/mesh/wg/data"
  client_compose="${STACK_DIR}/mesh/docker-compose.client.yml"

  if [ "${SERVER_N}" = "1" ]; then
    env_upsert "${file}" MESH_PEERS "4"
    env_upsert "${file}" SERVER_PUBLIC_IP "$(env_get SERVER_1_PUBLIC_IP)"
    ok "This is the hub. Peer configs land in ${wg_data}/ — send peerN.conf to each client."
  else
    if [ -z "${HUB_PUBKEY}" ]; then HUB_PUBKEY="$(env_get SERVER_1_WG_PUBLIC_KEY)"; fi
    if [ -z "${HUB_PUBKEY}" ]; then
      err "Hub (Server 1) WireGuard public key unknown."
      err "Run 'mesh.sh join' on Server 1 first, then re-run here with:"
      err "  scripts/mesh.sh join --server ${SERVER_N} --hub-pubkey <SERVER_1_WG_PUBLIC_KEY>"
      exit 1
    fi
    local hub_ip
    hub_ip="$(env_get SERVER_1_PUBLIC_IP)"
    [ -n "${hub_ip}" ] || hub_ip="${consul_addr}"

    mkdir -p "${wg_data}/wg_confs"
    cat > "${wg_data}/wg_confs/wg0.conf" <<EOF
# Static client config for server ${SERVER_N} (generated by scripts/mesh.sh)
[Interface]
PrivateKey = ${priv}
Address = ${mesh_ip}/32
DNS = 1.1.1.1

[Peer]
# Hub (Server 1)
PublicKey = ${HUB_PUBKEY}
Endpoint = ${hub_ip}:${MESH_PORT}
AllowedIPs = 10.10.0.0/16
PersistentKeepalive = 25
EOF
    ok "Wrote the client conf: ${wg_data}/wg_confs/wg0.conf"

    cat > "${client_compose}" <<EOF
# Generated by scripts/mesh.sh — client-mode mesh for non-hub servers.
# WireGuard dials the hub; Consul joins 10.10.1.1 over the tunnel.
name: innotel-mesh-client

services:
  wireguard:
    image: linuxserver/wireguard:latest
    container_name: mesh-wireguard
    cap_add: [NET_ADMIN, SYS_MODULE]
    environment:
      - PUID=0
      - PGID=0
      - TZ=UTC
      # Empty PEERS = client mode: the image keeps the mounted static
      # wg_confs/wg0.conf and starts the tunnel as a client (no peer gen).
      - PEERS=
    volumes:
      - ${wg_data}/wg_confs:/config/wg_confs
      - /lib/modules:/lib/modules
    sysctls:
      - net.ipv4.conf.all.src_valid_mark=1
      - net.ipv4.ip_forward=1
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wg", "show", "wg0"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks:
      - mesh-net

  consul:
    image: hashicorp/consul:1.19
    container_name: mesh-consul
    restart: unless-stopped
    volumes:
      - ${STACK_DIR}/mesh/registry/data:/consul/data
    ports:
      - "8500:8500"
      - "8600:8600/udp"
    command: >
      agent
        -bind=0.0.0.0
        -client=0.0.0.0
        -datacenter=innotel
        -data-dir=/consul/data
        -ui
        -retry-join=${consul_addr}
        ${consul_flag}
    healthcheck:
      test: ["CMD", "consul", "members"]
      interval: 15s
      timeout: 5s
      retries: 3
    networks:
      - mesh-net

networks:
  mesh-net:
    name: innotel-mesh-net
EOF
    ok "Wrote the client compose: ${client_compose}"
  fi

  # ── start + verify ─────────────────────────────────────────────────────────
  if [ "${SERVER_N}" = "1" ]; then
    ( cd "${STACK_DIR}" && ./stack.sh mesh )
  else
    docker compose -f "${client_compose}" up -d
  fi

  if [ "${NO_VERIFY}" = "1" ]; then
    ok "Mesh started on server ${SERVER_N} (verification skipped)."
    return 0
  fi

  info "Waiting for the tunnel (up to 60s)..."
  local tunnel_ok=0
  for _ in $(seq 1 30); do
    if docker exec mesh-wireguard wg show wg0 2>/dev/null | grep -q 'latest handshake'; then
      tunnel_ok=1; break
    fi
    sleep 2
  done
  if [ "${tunnel_ok}" = "1" ]; then
    ok "WireGuard tunnel: handshake established."
  else
    warn "No handshake yet — the hub must be up and reachable on UDP ${MESH_PORT}."
  fi

  if curl -s --max-time 5 "http://${consul_addr}:8500/v1/status/leader" | grep -q '"'; then
    ok "Consul reachable at ${consul_addr}:8500"
  else
    warn "Consul not reachable yet at ${consul_addr}:8500."
  fi

  ok "Server ${SERVER_N} joined the mesh."
}

# ── 2. leave ─────────────────────────────────────────────────────────────────
cmd_leave() {
  require_stack
  resolve_server

  local wg_data client_compose consul_addr
  wg_data="${STACK_DIR}/mesh/wg/data"
  client_compose="${STACK_DIR}/mesh/docker-compose.client.yml"
  consul_addr="10.10.1.1"
  [ "${SERVER_N}" = "1" ] && consul_addr="10.10.${SERVER_N}.1"

  # Refuse to yank the tunnel out from under a running stack unless --purge.
  if [ "${PURGE}" != "1" ] && docker ps --format '{{.Names}}' 2>/dev/null \
       | grep -qE "^g${SERVER_N}-"; then
    warn "Group ${SERVER_N} services are still running on this host."
    warn "Stop them first (${STACK_DIR}/stack.sh down ${SERVER_N}) — continuing anyway."
  fi

  if [ "${DRY_RUN}" = "1" ]; then
    if [ "${PURGE}" = "1" ]; then
      warn "DRY-RUN — would stop the mesh and purge its state."
    else
      warn "DRY-RUN — would stop the mesh."
    fi
    return 0
  fi

  if [ "${SERVER_N}" = "1" ]; then
    warn "Server 1 is the hub — stopping the mesh disconnects every other node."
    ( cd "${STACK_DIR}" && ./stack.sh down mesh ) 2>/dev/null \
      || docker compose -f "${STACK_DIR}/mesh/docker-compose.mesh.yml" down 2>/dev/null || true
  elif [ -f "${client_compose}" ]; then
    docker compose -f "${client_compose}" down 2>/dev/null || true
  else
    docker compose -f "${STACK_DIR}/mesh/docker-compose.mesh.yml" down 2>/dev/null || true
  fi
  ok "Mesh stopped on server ${SERVER_N}."

  # Deregister whatever this node advertised (best effort).
  local svc
  for svc in $(curl -s --max-time 5 "http://${consul_addr}:8500/v1/agent/services" 2>/dev/null \
                 | grep -oP '^\s*"\K[^"]+(?=":)' || true); do
    curl -s -X PUT "http://${consul_addr}:8500/v1/agent/service/deregister/${svc}" >/dev/null 2>&1 || true
  done
  ok "Deregistered this node's Consul services."

  if [ "${PURGE}" = "1" ]; then
    rm -rf "${wg_data}" "${STACK_DIR}/mesh/registry/data"
    ok "Purged mesh state (WireGuard configs + Consul data)."
  else
    info "State kept in ${wg_data}. Re-run with --purge to remove it."
  fi
}

# ── 3. download ──────────────────────────────────────────────────────────────
# Which repos to act on: explicit names, or the selected group / everything.
resolve_selection() {
  if [ "${#SELECT_REPOS[@]}" -gt 0 ]; then
    printf '%s\n' "${SELECT_REPOS[@]}"
    return
  fi
  if [ "${SELECT_ALL}" = "1" ]; then
    all_repos | tr ' ' '\n' | sed '/^$/d'
    return
  fi
  local n="${SELECT_GROUP:-}"
  [ -n "${n}" ] || n="$(detect_server)"
  if [ -n "${n}" ]; then
    group_repos "${n}" | tr ' ' '\n' | sed '/^$/d'
    return
  fi
  # No group context: just this repo.
  echo "${REPO_NAME}"
}

require_workspace() {
  [ -n "${DEV_ROOT}" ] && [ -d "${DEV_ROOT}" ] \
    || die "No workspace root — run from a group checkout or set MESH_DEV_ROOT=/path/to/root."
}

cmd_download() {
  require_workspace
  local base="${MESH_GIT_BASE:-https://github.com/innotelinc}"
  local any=0

  while read -r name; do
    [ -n "${name}" ] || continue
    any=1
    local n dir slug url
    n="$(group_of_repo "${name}")"
    if [ -n "${n}" ]; then
      dir="${DEV_ROOT}/$(group_name_for "${n}" | sed "s|^|${n}-|")/${name}"
    else
      dir="${DEV_ROOT}/${name}"
    fi
    slug="$(repo_slug "${name}")"
    url="${base}/${slug}.git"

    if [ -d "${dir}/.git" ]; then
      if [ "${DO_PULL}" = "1" ]; then
        if [ "${DRY_RUN}" = "1" ]; then
          info "DRY-RUN — would pull ${name}"
        elif git -C "${dir}" pull --ff-only >/dev/null 2>&1; then
          ok "${name} — updated"
        else
          warn "${name} — pull failed (dirty tree or no upstream); left as is"
        fi
      else
        ok "${name} — present ($(basename "$(dirname "${dir}")")/${name})"
      fi
      continue
    fi

    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would clone ${url} → ${dir}"
      continue
    fi
    mkdir -p "$(dirname "${dir}")"
    if git clone --depth 1 "${url}" "${dir}" >/dev/null 2>&1; then
      ok "${name} — cloned"
    else
      err "${name} — clone failed (${url}); over the mesh, set MESH_GIT_BASE to your Gitea"
    fi
  done < <(resolve_selection)

  [ "${any}" = "1" ] || warn "Nothing selected."
}

# ── 4. install ───────────────────────────────────────────────────────────────
cmd_install() {
  have git || die "git is required."
  require_stack
  require_workspace
  resolve_server

  local gname; gname="$(group_name_for "${SERVER_N}")"
  info "Installing server ${SERVER_N} (${gname}) into ${DEV_ROOT}"

  # 1. Group dirs.
  local n
  for n in 1 2 3 4 5; do
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would ensure ${DEV_ROOT}/${n}-$(group_name_for "$n")/"
    else
      mkdir -p "${DEV_ROOT}/${n}-$(group_name_for "$n")"
    fi
  done

  # 2. Member repos for this server (respecting any narrower selection).
  if [ "${#SELECT_REPOS[@]}" -eq 0 ] \
     && [ "${SELECT_ALL}" != "1" ] && [ -z "${SELECT_GROUP}" ]; then
    SELECT_GROUP="${SERVER_N}"
  fi
  cmd_download

  # 3. Join the mesh.
  if [ "${DO_JOIN}" = "1" ]; then
    cmd_join
  else
    info "Skipping the mesh join — re-run with --join when this host's keypair is ready."
  fi

  # 4. Optional bring-up.
  if [ "${DO_UP}" = "1" ]; then
    [ "${DRY_RUN}" = "1" ] || ( cd "${STACK_DIR}" && ./stack.sh up "${SELECT_GROUP:-${SERVER_N}}" )
  fi

  ok "Server ${SERVER_N} install complete."
  info "Next: ${STACK_DIR}/stack.sh up ${SERVER_N}   ·   ${STACK_DIR}/stack.sh status"
}

# ── 5. deploy — the install, driven onto a remote host over SSH ─────────────
# Nothing is installed on this host: every step runs on the target through ssh,
# so a bare machine needs only sshd + git (docker too for --join/--up).
# Credentials are generated *here*, written into the target's .env files, printed
# once at the end, and (with --vault) pushed to the platform Vault — one path per
# component.

ssh_do() { # remote shell command line (built here, run by the target's shell)
  [ -n "${DEPLOY_HOST}" ] || die "deploy requires --host [user@]host."
  local opts=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
  [ -n "${SSH_KEY}" ] && opts+=(-i "${SSH_KEY}")
  ssh "${opts[@]}" -p "${SSH_PORT}" "${DEPLOY_HOST}" "$*"
}

q() { printf '%q' "$1"; }

group_num_for_name() { # voice -> 2
  case "$1" in
    1|primary) echo 1 ;; 2|voice) echo 2 ;; 3|media) echo 3 ;;
    4|social)  echo 4 ;; 5|dev)   echo 5 ;;
    *) echo "" ;;
  esac
}

extension_names() { # ips/extensions/<name>/ext.yml
  [ -n "${STACK_DIR}" ] || return 0
  local d
  for d in "${STACK_DIR}"/extensions/*/; do
    [ -f "${d}ext.yml" ] && basename "${d}"
  done
}

component_list() { # "<kind>|<group>|<name>" — roster repos, then stack extensions
  local n name
  for n in 1 2 3 4 5; do
    for name in $(group_repos "${n}"); do printf 'repo|%s|%s\n' "${n}" "${name}"; done
  done
  for name in $(extension_names); do printf 'ext|-|%s\n' "${name}"; done
}

select_components() {
  local -a lines=()
  local line
  while IFS= read -r line; do [ -n "${line}" ] && lines+=("${line}"); done < <(component_list)
  [ "${#lines[@]}" -gt 0 ] || die "No components known — is the ips stack checked out (--stack DIR)?"

  local spec="${COMPONENTS}"
  if [ -z "${spec}" ]; then
    # %b, not %s: BOLD/NC carry literal `\033` escapes and only printf expands them.
    printf '\n%bComponents%b\n' "${BOLD}" "${NC}"
    local i kind n name last=""
    for i in "${!lines[@]}"; do
      IFS='|' read -r kind n name <<<"${lines[$i]}"
      if [ "${kind}" = "repo" ] && [ "${n}" != "${last}" ]; then
        printf '\n  %b%s-%s%b\n' "${BOLD}" "${n}" "$(group_name_for "${n}")" "${NC}"
        last="${n}"
      elif [ "${kind}" = "ext" ] && [ "${last}" != "ext" ]; then
        printf '\n  %bstack extensions%b\n' "${BOLD}" "${NC}"
        last="ext"
      fi
      printf '   %3d) %-5s %s\n' "$((i + 1))" "${kind}" "${name}"
    done
    printf '\nSelect (1,3,5-7 · all · repos · exts · 2/voice · names): '
    read -r spec
    [ -n "${spec}" ] || die "Nothing selected."
  fi

  local token want found r
  for token in $(printf '%s' "${spec}" | tr ',' ' '); do
    case "${token}" in
      all) SELECT_COMPONENTS=("${lines[@]}"); return 0 ;;
      repos)
        for line in "${lines[@]}"; do [[ "${line}" == repo\|* ]] && SELECT_COMPONENTS+=("${line}"); done
        ;;
      exts|extensions)
        for line in "${lines[@]}"; do [[ "${line}" == ext\|* ]] && SELECT_COMPONENTS+=("${line}"); done
        ;;
      [1-9]|[1-9][0-9]) # a menu number when the spec is a plain list, else server N
        if [ -n "${COMPONENTS}" ] && [ "${token}" -le 5 ]; then
          for line in "${lines[@]}"; do [[ "${line}" == repo\|${token}\|* ]] && SELECT_COMPONENTS+=("${line}"); done
        else
          [ "${token}" -le "${#lines[@]}" ] && SELECT_COMPONENTS+=("${lines[$((token - 1))]}")
        fi
        ;;
      [0-9]-[0-9]*|[0-9]*-[0-9]*) # menu range a-b
        local from="${token%%-*}" to="${token##*-}"
        for ((i = from; i <= to; i++)); do
          [ "${i}" -ge 1 ] && [ "${i}" -le "${#lines[@]}" ] && SELECT_COMPONENTS+=("${lines[$((i - 1))]}")
        done
        ;;
      */*) # group/name, e.g. 2/capstone
        local g="${token%%/*}" nm="${token##*/}"
        g="$(group_num_for_name "${g}")"
        for line in "${lines[@]}"; do
          IFS='|' read -r kind n name <<<"${line}"
          [[ "${name}" == "${nm}" && -z "${g}" || "${name}" == "${nm}" && "${n}" == "${g}" ]] && SELECT_COMPONENTS+=("${line}")
        done
        ;;
      *)
        found=0
        for line in "${lines[@]}"; do
          IFS='|' read -r kind n name <<<"${line}"
          if [ "${name}" = "${token}" ]; then SELECT_COMPONENTS+=("${line}"); found=1; fi
        done
        [ "${found}" = "1" ] || warn "Unknown component '${token}' — skipped"
        ;;
    esac
  done

  # Dedupe (a group and an explicit name overlap all the time).
  local -a uniq=()
  for line in "${SELECT_COMPONENTS[@]:-}"; do
    [ -n "${line}" ] || continue
    want=1
    for r in "${uniq[@]:-}"; do [ "${r}" = "${line}" ] && want=0; done
    [ "${want}" = "1" ] && uniq+=("${line}")
  done
  SELECT_COMPONENTS=("${uniq[@]:-}")
  [ "${#SELECT_COMPONENTS[@]}" -gt 0 ] || die "Nothing selected."
}

deploy_server() { # the *target's* server number (never this host's)
  if [ -n "${GROUP_NUM}" ]; then SERVER_N="${GROUP_NUM}"; return 0; fi
  local line kind n name
  for line in "${SELECT_COMPONENTS[@]}"; do
    IFS='|' read -r kind n name <<<"${line}"
    if [ "${kind}" = "repo" ]; then
      SERVER_N="${n}"
      info "Server ${SERVER_N} (group ${n}-$(group_name_for "${n}")) inferred from '${name}' — override with --server N"
      return 0
    fi
  done
  die "Extensions only — pass --server N so the mesh layout is unambiguous."
}

deploy_preflight() {
  info "Target ${DEPLOY_HOST} (ssh port ${SSH_PORT})"
  if [ "${DRY_RUN}" = "1" ]; then
    warn "DRY-RUN — skipping the ssh probe of ${DEPLOY_HOST}"
    return 0
  fi
  ssh_do 'true' || die "Cannot ssh to ${DEPLOY_HOST} (BatchMode: use a key, or --ssh-key FILE)."
  if [ "$(ssh_do 'id -u')" = "0" ]; then
    ok "root on the target"
  else
    info "non-root login (docker needs the target user in the docker group)"
  fi
  local tool missing=0
  for tool in git docker; do
    if ssh_do "command -v ${tool} >/dev/null 2>&1"; then ok "${tool} present"; else warn "${tool} missing on the target"; [ "${tool}" = git ] && missing=1; fi
  done
  [ "${missing}" = "0" ] || die "Install git on ${DEPLOY_HOST} first."
}

gen_secret() { # [len]
  local len="${1:-28}"
  if have openssl; then
    openssl rand -base64 $((len * 2)) | tr -dc 'A-Za-z0-9' | cut -c1-"${len}"
  else
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "${len}"
  fi
}

# Only keys whose *name* says secret and whose sample value is empty or a
# placeholder are touched — a real value in .env.example is left alone.
SECRET_KEY_RE='(PASSWORD|PASS|SECRET|TOKEN|KEY|SALT|CREDENTIAL|WEBHOOK)'
PLACEHOLDER_RE='(change-me|changeme|change_me|placeholder|example|your[-_]|some-|xxx|todo|hunter2|^$|^secret$|^password$)'

remote_env_set() { # dir key value
  local dir="$1" key="$2" value="$3"
  ssh_do "f=$(q "${dir}/.env"); if grep -q '^${key}=' \"\$f\"; then sed -i 's|^${key}=.*|${key}=${value}|' \"\$f\"; else printf '%s=%s\n' ${key} ${value} >> \"\$f\"; fi"
}

deploy_env_credentials() { # dir component
  local dir="$1" component="$2"
  local example
  example="$(ssh_do "cat $(q "${dir}/.env.example") 2>/dev/null" || true)"
  if [ -z "${example}" ]; then info "${component}: no .env.example — nothing to seed"; return 0; fi
  [ "${DRY_RUN}" = "1" ] || ssh_do "test -f $(q "${dir}/.env") || cp $(q "${dir}/.env.example") $(q "${dir}/.env")"

  local line key value gen
  local -a pairs=()
  local -a keys=()
  while IFS= read -r line; do
    case "${line}" in '#'*|'') continue ;; esac
    key="${line%%=*}"; value="${line#*=}"
    [[ "${key}" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    [[ "${key}" =~ ${SECRET_KEY_RE} ]] || continue
    [[ "${value}" =~ ${PLACEHOLDER_RE} ]] || continue
    gen="$(gen_secret 28)"
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would set ${component}:${key}"
    else
      remote_env_set "${dir}" "${key}" "${gen}" || warn "${component}: could not write ${key}"
    fi
    pairs+=("${key}=${gen}")
    keys+=("${key}")
    CRED_NAMES+=("${component}:${key}")
    CRED_VALUES+=("${gen}")
  done <<<"${example}"

  if [ "${#keys[@]}" -gt 0 ]; then
    ok "${component}: ${#keys[@]} credential(s) generated — ${keys[*]}"
    vault_push "${component}" "${pairs[@]}"
  else
    info "${component}: no placeholder secrets in .env.example"
  fi
}

vault_push() { # component KEY=VALUE...
  local component="$1"; shift
  [ "${DO_VAULT}" = "1" ] || return 0
  if [ -z "${VAULT_ADDR:-}" ]; then warn "VAULT_ADDR unset — not pushing ${component}"; return 0; fi
  local token="${VAULT_TOKEN:-}"
  if [ -z "${token}" ] && [ -n "${VAULT_TOKEN_FILE:-}" ] && [ -f "${VAULT_TOKEN_FILE}" ]; then
    token="$(cat "${VAULT_TOKEN_FILE}")"
  fi
  if [ -z "${token}" ]; then warn "No Vault token (VAULT_TOKEN / VAULT_TOKEN_FILE) — not pushing ${component}"; return 0; fi
  have python3 || { warn "python3 not found — cannot build the Vault payload for ${component}"; return 0; }

  local prefix="${VAULT_PREFIX:-cerulean}" path="${VAULT_PATH_OVERRIDE:-${component}}"
  local -a curl_args=(-fsS -X POST)
  [ -n "${VAULT_NAMESPACE:-}" ] && curl_args+=(-H "X-Vault-Namespace: ${VAULT_NAMESPACE}")
  [ "${VAULT_SKIP_VERIFY:-}" = "1" ] && curl_args+=(-k)

  local payload
  payload="$(printf '%s\n' "$@" | python3 -c 'import json, sys
kv = {}
for line in sys.stdin.read().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        kv[k] = v
print(json.dumps({"data": kv}))')" || { warn "${component}: could not build the payload"; return 0; }

  local url="${VAULT_ADDR%/}/v1/${prefix}/data/${path}"
  if [ "${DRY_RUN}" = "1" ]; then warn "DRY-RUN — would write $(($#)) key(s) to ${url}"; return 0; fi
  if curl "${curl_args[@]}" "${url}" -H "X-Vault-Token: ${token}" --data "${payload}" >/dev/null; then
    ok "${component}: stored in Vault at ${prefix}/data/${path}"
  else
    warn "${component}: Vault write failed (${url})"
  fi
}

cmd_deploy() {
  have ssh || die "ssh is required for deploy."
  require_stack
  [ -n "${DEPLOY_HOST}" ] || die "Usage: mesh.sh deploy --host [user@]host [--components LIST]"

  select_components
  deploy_server
  deploy_preflight

  local root="${DEPLOY_ROOT}" n
  info "Workspace root on the target: ${root}"
  local kind name slug dir ids=()
  for n in 1 2 3 4 5; do
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would ensure ${root}/${n}-$(group_name_for "$n")/"
    else
      ssh_do "mkdir -p $(q "${root}/${n}-$(group_name_for "$n")")"
    fi
  done

  local any_repo=0
  for ids in "${SELECT_COMPONENTS[@]}"; do
    IFS='|' read -r kind n name <<<"${ids}"
    if [ "${kind}" = "ext" ]; then
      info "extension '${name}' — enabled on the target with stack.sh enable ${name} ${SERVER_N}"
      continue
    fi
    any_repo=1
    slug="$(repo_slug "${name}")"
    dir="${root}/${n}-$(group_name_for "${n}")/${name}"
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would clone ${MESH_GIT_BASE:-https://github.com/innotelinc}/${slug}.git → ${DEPLOY_HOST}:${dir}"
      continue
    fi
    if ssh_do "test -d $(q "${dir}/.git")"; then
      if ssh_do "git -C $(q "${dir}") pull --ff-only"; then ok "${name} — updated"; else warn "${name} — pull failed; left as is"; fi
    elif ssh_do "mkdir -p $(q "$(dirname "${dir}")") && git clone --depth 1 $(q "${MESH_GIT_BASE:-https://github.com/innotelinc}/${slug}.git") $(q "${dir}")"; then
      ok "${name} — cloned to ${dir}"
    else
      err "${name} — clone failed (${MESH_GIT_BASE:-https://github.com/innotelinc}/${slug}.git)"; continue
    fi
    deploy_env_credentials "${dir}" "${name}"
  done

  # Extensions are enabled through the stack on the target (it owns the group
  # composes), so they need the stack checkout there too.
  if [ "${any_repo}" = "1" ] && [ "${DO_UP}" = "1" ]; then
    local stack_dir="${root}/ips"
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would run ${stack_dir}/stack.sh up ${SERVER_N} on the target"
    else
      ssh_do "test -d $(q "${stack_dir}/.git") || git clone --depth 1 $(q "${MESH_GIT_BASE:-https://github.com/innotelinc}/innotel-platform-stack.git") $(q "${stack_dir}")"
      for ids in "${SELECT_COMPONENTS[@]}"; do
        IFS='|' read -r kind n name <<<"${ids}"
        [ "${kind}" = "ext" ] && ssh_do "cd $(q "${stack_dir}") && ./stack.sh enable ${name} ${SERVER_N}"
      done
      ssh_do "cd $(q "${stack_dir}") && ./stack.sh up ${SERVER_N}"
    fi
  fi

  if [ "${DO_JOIN}" = "1" ]; then
    local join_repo
    join_repo="${root}/${SERVER_N}-$(group_name_for "${SERVER_N}")/$(group_repos "${SERVER_N}" | awk '{print $1}')"
    if [ "${DRY_RUN}" = "1" ]; then
      info "DRY-RUN — would run ${join_repo}/scripts/mesh.sh join --server ${SERVER_N} on the target"
    else
      ssh_do "cd $(q "${join_repo}") && ./scripts/mesh.sh join --server ${SERVER_N}"
    fi
  fi

  ok "Deploy to ${DEPLOY_HOST} complete."
  if [ "${#CRED_NAMES[@]}" -gt 0 ]; then
    printf '\n%bGenerated credentials%b (also written to the target .env files)\n' "${BOLD}" "${NC}"
    local i
    for i in "${!CRED_NAMES[@]}"; do printf '  %-46s %s\n' "${CRED_NAMES[$i]}" "${CRED_VALUES[$i]}"; done
    if [ -n "${CRED_FILE}" ]; then
      {
        printf '# credentials generated by mesh.sh deploy for %s on %s\n' "${DEPLOY_HOST}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        for i in "${!CRED_NAMES[@]}"; do printf '%s=%s\n' "${CRED_NAMES[$i]}" "${CRED_VALUES[$i]}"; done
      } > "${CRED_FILE}"
      printf '\nwrote %s (mode 600)\n' "${CRED_FILE}"
      chmod 600 "${CRED_FILE}" 2>/dev/null || true
    fi
  else
    warn "No credentials were generated — check the components' .env.example files."
  fi
  info "Next: ssh ${DEPLOY_HOST} 'cd ${root}/ips && ./stack.sh status'"
}

# ── status / discover ────────────────────────────────────────────────────────
cmd_status() {
  find_stack
  local name n
  n="$(detect_server)"
  name="${REPO_NAME}"
  printf "\n${BOLD}═══ Innotel mesh — this host ═══${NC}\n\n"
  printf '  repo        %s\n' "${name}"
  printf '  workspace   %s\n' "${DEV_ROOT:-(standalone checkout)}"
  printf '  stack       %s\n' "${STACK_DIR:-(not found)}"
  printf '  server      %s\n' "${n:-unknown}"
  if [ -n "${n}" ]; then
    printf '  group       %s\n' "$(group_name_for "${n}")"
    printf '  mesh IP     10.10.%s.1\n' "${n}"
    printf '  runs        %s\n' "$(group_repos "${n}")"
  fi
  printf '\n'

  if have docker && docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^mesh-wireguard$'; then
    ok "WireGuard container is up"
    docker exec mesh-wireguard wg show wg0 2>/dev/null | sed 's/^/  /' || true
  else
    warn "Mesh is not running on this host."
  fi

  local consul="10.10.1.1"
  [ -n "${n}" ] && [ "${n}" = "1" ] && consul="10.10.1.1"
  if curl -s --max-time 5 "http://${consul}:8500/v1/status/leader" | grep -q '"'; then
    ok "Consul leader: $(curl -s --max-time 5 "http://${consul}:8500/v1/status/leader")"
  else
    warn "Consul not reachable at ${consul}:8500"
  fi
}

cmd_discover() {
  local svc="${1:-}"
  [ -n "${svc}" ] || die "Usage: mesh.sh discover <service>"
  local consul="${MESH_CONSUL_ADDR:-10.10.1.1:8500}"
  info "Looking up '${svc}' in Consul (${consul})..."
  local out
  out="$(curl -s --max-time 5 "http://${consul}/v1/health/service/${svc}?passing=true" || true)"
  if [ -z "${out}" ] || [ "${out}" = "[]" ]; then
    warn "'${svc}' is not registered (or not passing) in the mesh."
    return 1
  fi
  printf '%s\n' "${out}" \
    | grep -oP '"ServiceAddress":"\K[^"]+|\"ServicePort\":\K[0-9]+' \
    | paste - - || printf '%s\n' "${out}"
}

# ── help / dispatch ──────────────────────────────────────────────────────────
usage() {
  # The block comment at the top of this file IS the help text: print it up to
  # the first line of code.
  awk 'NR==1 { next } /^set -euo pipefail/ { exit } { sub(/^# ?/, ""); print }' \
    "${BASH_SOURCE[0]}"
}

main() {
  [ "$#" -gt 0 ] || { usage; exit 1; }
  local cmd="$1"; shift

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --server)     GROUP_NUM="$2"; shift 2 ;;
      --group)      SELECT_GROUP="$2"; GROUP_NUM="$2"; shift 2 ;;
      --hub-pubkey) HUB_PUBKEY="$2"; shift 2 ;;
      --stack)      MESH_STACK_DIR="$2"; shift 2 ;;
      --root)       MESH_DEV_ROOT="$2"; DEV_ROOT="$2"; shift 2 ;;
      --all)        SELECT_ALL=1; shift ;;
      --pull)       DO_PULL=1; shift ;;
      --join)       DO_JOIN=1; shift ;;
      --up)         DO_UP=1; shift ;;
      --purge)      PURGE=1; shift ;;
      --no-verify)  NO_VERIFY=1; shift ;;
      --host)       DEPLOY_HOST="$2"; shift 2 ;;
      --ssh-port)   SSH_PORT="$2"; shift 2 ;;
      --ssh-key)    SSH_KEY="$2"; shift 2 ;;
      --components) COMPONENTS="$2"; shift 2 ;;
      --credentials-file) CRED_FILE="$2"; shift 2 ;;
      --vault)      DO_VAULT=1; shift ;;
      --vault-path) DO_VAULT=1; VAULT_PATH_OVERRIDE="$2"; shift 2 ;;
      --dry-run)    DRY_RUN=1; shift ;;
      -h|--help)    usage; exit 0 ;;
      -*)           die "Unknown option: $1" ;;
      *)            SELECT_REPOS+=("$1"); shift ;;
    esac
  done

  case "${cmd}" in
    join)     cmd_join ;;
    leave)    cmd_leave ;;
    download) cmd_download ;;
    install)  cmd_install ;;
    deploy)   cmd_deploy ;;
    status)   cmd_status ;;
    discover) cmd_discover "${SELECT_REPOS[@]:-}" ;;
    help|-h|--help) usage ;;
    *)        err "Unknown command: ${cmd}"; usage; exit 1 ;;
  esac
}

main "$@"
