#!/usr/bin/env bash
# install-build-runner.sh — install the host-side build runner as a systemd service.
#
# Studio's "Build it" cannot run `make app` itself: the Studio image is a traced
# Next.js bundle with no Archon CLI, no Codex CLI, no `uv` and no checkout. It
# drops a request into the queue directory instead, and this service — running
# where the toolchain actually is — executes the same command `make app` and CI
# run (`scripts/manufacture.sh`) and records the outcome where Studio can read it.
#
# Writes one unit into /etc/systemd/system and starts it:
#
#   olympus-build-runner.service   scripts/build-runner.py --serve
#
# WHY THIS UNIT IS NOT HARDENED LIKE THE CHECK UNITS. `olympus-*-check.service`
# runs read-only wrappers, so it can afford ProtectSystem=strict and a read-only
# home. This one exists to run a coding agent: it writes apps into ./builds/,
# keeps Archon state under ~/.archon and ~/.cache, and talks to the model
# gateway. Copying the read-only block here would make the first build fail with
# EROFS and read as a workflow bug. The containment that matters is in the
# runner itself, which treats the queue as untrusted input.
#
# WHICH ACCOUNT RUNS THE BUILD — probed, because the honest answer depends on the
# host. A build runs a coding agent, and the agent works inside Codex's
# bubblewrap sandbox. bubblewrap needs user namespaces, and plenty of container
# environments (this one included) deny those to unprivileged users. There, a
# non-root runner does not make builds safer — it makes the sandbox unavailable,
# so the agent executes unsandboxed with whatever the account can read, which is
# the opposite of containment. So:
#
#   unprivileged user namespaces work -> run as $BUILD_USER  (sandboxed, no root)
#   denied                            -> run as root          (sandboxed by bwrap)
#
# Both keep the agent sandboxed; only one of them also keeps the runner off root.
# The installer probes with the build account itself and reports which it chose and
# why. `--as-user` / `--as-root` override the probe; `--as-user` on a host that
# denies namespaces means an unsandboxed agent, and the script says so.
#
# When it does run as the build account, three pieces of state are handed over so a
# fresh install works on the first click:
#
#   ./builds/             chowned to the builder — it is the only writer
#   .factory/build-queue/ group-writable, with an ACL for Studio's uid (1001),
#                         which has no host account to put in the group
#   .env                  reads as 0600 root, so the builder gets an ACL rather
#                         than a widened mode: the file holds the Vault and
#                         Authentik secrets and only the runner needs those keys
#
#   scripts/install-build-runner.sh              # install and start (probes)
#   scripts/install-build-runner.sh --uninstall
#   scripts/install-build-runner.sh --no-start   # write the unit only
#   scripts/install-build-runner.sh --as-user    # force the build account
#   scripts/install-build-runner.sh --as-root    # force root
#   systemctl status olympus-build-runner
#   journalctl -u olympus-build-runner -f
#   python3 scripts/build-runner.py --list
set -euo pipefail

DIR=/etc/systemd/system
UNIT=olympus-build-runner
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
BUILD_USER="${BUILD_USER:-olympus-builder}"
BUILD_GROUP="${BUILD_GROUP:-olympus-build}"
BUILD_HOME="${BUILD_HOME:-/var/lib/$BUILD_USER}"
STUDIO_UID="${STUDIO_UID:-1001}"

say()  { printf '\033[1;32m==> \033[0m%s\n' "$*"; }
warn() { printf '\033[1;33m==> \033[0m%s\n' "$*" >&2; }
die()  { printf '\033[1;31m==> \033[0m%s\n' "$*" >&2; exit 1; }

uninstall=false
start=true
account_pref=auto
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=true ;;
        --no-start)  start=false ;;
        --as-root)   account_pref=root ;;
        --as-user)   account_pref=user ;;
        *) die "unknown argument: $arg (expected --uninstall, --no-start, --as-user and/or --as-root)" ;;
    esac
done
run_as_root=false

if $uninstall; then
    systemctl disable --now "$UNIT.service" 2>/dev/null || true
    rm -f "$DIR/$UNIT.service"
    systemctl reset-failed "$UNIT.service" 2>/dev/null || true
    say "removed the $UNIT unit (the queue and any built apps are untouched)"
    exit 0
fi

[[ -f "$REPO_ROOT/scripts/build-runner.py" ]] || die "scripts/build-runner.py is missing next to this installer"

[[ $(id -u) -eq 0 ]] || die "needs root to write $DIR and start the service (try sudo)"

command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found — set PYTHON=<path> or install python3"

# The runner writes its own requests' status files, but Studio has to be able to
# write *requests* into the same directory, so it has to be owned by Studio's uid
# before the service ever starts. Doing it here means a fresh install works on
# the first click rather than at the first 503.
if [[ -x "$REPO_ROOT/scripts/studio-export-dir.sh" ]]; then
    STUDIO_DIR_LABEL="build-queue" \
        "$REPO_ROOT/scripts/studio-export-dir.sh" "$REPO_ROOT/.factory/build-queue" \
        || warn "could not make the queue writable by Studio's uid — 'Build it' will answer 503"
else
    mkdir -p "$REPO_ROOT/.factory/build-queue"
fi

# ---- the build account --------------------------------------------------------
# The account is created either way: it is what the probe runs as, and it is what a
# later `--as-user` needs.
getent group "$BUILD_GROUP" >/dev/null 2>&1 || groupadd --system "$BUILD_GROUP"
if ! id -u "$BUILD_USER" >/dev/null 2>&1; then
    useradd --system --gid "$BUILD_GROUP" --home-dir "$BUILD_HOME" \
        --shell /usr/sbin/nologin --comment "Olympus app builds" "$BUILD_USER"
    say "created service account $BUILD_USER ($BUILD_GROUP)"
fi
install -d -o "$BUILD_USER" -g "$BUILD_GROUP" -m 0750 "$BUILD_HOME"

# The probe is the whole decision: can this account, on this host, create a user
# namespace? If not, bubblewrap cannot start and Codex's `workspace-write` sandbox
# is unavailable to it.
userns_ok=false
if command -v runuser >/dev/null 2>&1 && command -v unshare >/dev/null 2>&1; then
    if runuser -u "$BUILD_USER" -- unshare -U true 2>/dev/null; then
        userns_ok=true
    fi
fi

case "$account_pref" in
    root) run_as_root=true ;;
    user) run_as_root=false ;;
    auto)
        if $userns_ok; then
            run_as_root=false
        else
            run_as_root=true
            warn "unprivileged user namespaces are denied here, so bubblewrap cannot run as"
            warn "  $BUILD_USER — Codex's sandbox would be unavailable and the agent would"
            warn "  execute unsandboxed. Installing as root instead, which keeps the agent"
            warn "  sandboxed. Enable unprivileged user namespaces (or pass --as-user, with"
            warn "  the sandbox then disabled) to run builds as $BUILD_USER."
        fi
        ;;
esac

if ! $run_as_root && $userns_ok; then
    say "build account: $BUILD_USER (user namespaces available — agent stays sandboxed)"
elif ! $run_as_root; then
    warn "--as-user: builds run as $BUILD_USER, but bubblewrap cannot start for it, so the"
    warn "  agent runs WITHOUT its sandbox. Containment is then this account's permissions."
fi

if $run_as_root; then
    SERVICE_USER=root
    SERVICE_GROUP=root
    SERVICE_HOME=/root
    SERVICE_PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
else
    SERVICE_USER="$BUILD_USER"
    SERVICE_GROUP="$BUILD_GROUP"
    SERVICE_HOME="$BUILD_HOME"
    SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    # `uv` is usually a single binary in the operator's own ~/.local/bin, which the
    # service account cannot read (and /root is 0700). "Is uv on *my* PATH" is the
    # wrong question — under sudo, root's PATH resolves it and the install looks
    # fine while the builder still cannot run the workflow's `runtime: uv` nodes. So
    # the test is whether a copy exists where every account can execute it.
    if [[ ! -x /usr/local/bin/uv ]]; then
        candidates=()
        command -v uv >/dev/null 2>&1 && candidates+=("$(command -v uv)")
        candidates+=(/root/.local/bin/uv "$SERVICE_HOME/.local/bin/uv")
        for candidate in "${candidates[@]}"; do
            if [[ -x "$candidate" ]]; then
                install -m 0755 "$candidate" /usr/local/bin/uv
                say "published uv: $candidate -> /usr/local/bin/uv (readable by every account)"
                break
            fi
        done
    fi
    if [[ ! -x /usr/local/bin/uv ]]; then
        warn "uv is not available to the service account — the workflow's script nodes declare 'runtime: uv'; install it (see docs/stack.md)"
    fi

    # ./builds/ — the builder is its only writer, so it owns it outright.
    # -R, because apps built before this change are root-owned inside, and the
    # `replace` path removes a previous build before rebuilding: it would fail to,
    # silently, and the workflow's clobber guard would then refuse the rebuild.
    if [[ -d "$REPO_ROOT/builds" ]] || mkdir -p "$REPO_ROOT/builds"; then
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$REPO_ROOT/builds"
        chmod 0775 "$REPO_ROOT/builds"
    fi

    # The queue is written by BOTH accounts: Studio drops requests in, the builder
    # renames them and writes status/log files. Studio runs as uid 1001 with no
    # host account, so it cannot simply be added to the group — it gets an ACL,
    # and a *default* ACL so files either side creates stay readable to the other.
    QUEUE_DIR="$REPO_ROOT/.factory/build-queue"
    chgrp "$SERVICE_GROUP" "$QUEUE_DIR"
    chmod 2775 "$QUEUE_DIR"
    if command -v setfacl >/dev/null 2>&1; then
        setfacl -m "u:$SERVICE_USER:rwx,u:$STUDIO_UID:rwx" "$QUEUE_DIR"
        setfacl -d -m "u:$SERVICE_USER:rwx,u:$STUDIO_UID:rwx" "$QUEUE_DIR"
        # -R, and it matters: a default ACL only governs files created later. A
        # queue left over from a root install already holds runner.lock and a
        # heartbeat, both root-owned 0644 — the runner then fails to open its own
        # lock and the unit restart-loops. `X` keeps directories traversable
        # without marking files executable.
        setfacl -R -m "u:$SERVICE_USER:rwX,u:$STUDIO_UID:rwX" "$QUEUE_DIR"
        say "queue shared: $QUEUE_DIR — group $SERVICE_GROUP + uid $STUDIO_UID (ACL)"
    else
        warn "setfacl is missing; Studio (uid $STUDIO_UID) may not be able to queue builds"
    fi

    # `.env` holds the gateway key the runner allow-lists — and the Vault and
    # Authentik secrets it must never pass on. An ACL keeps the 0600 mode intact.
    if [[ -f "$REPO_ROOT/.env" ]] && command -v setfacl >/dev/null 2>&1; then
        setfacl -m "u:$SERVICE_USER:r" "$REPO_ROOT/.env"
    fi
fi

cat > "$DIR/$UNIT.service" <<UNIT
[Unit]
Description=Olympus build runner — runs \`make app\` for builds queued by Studio
# The gateway is a container on this compose project; waiting for docker keeps a
# boot-time queue from starting against an endpoint that is not listening yet.
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$REPO_ROOT
# The runner reads the repo .env itself, allow-listing OMNIROUTE_*/ARCHON_* — so
# unlike EnvironmentFile= it never hands the build the Vault or Authentik tokens.
# The unit therefore carries no credential and no host name.
ExecStart=$PYTHON $REPO_ROOT/scripts/build-runner.py --serve
Restart=always
RestartSec=5
# Home is the service account's own, so Archon state lands in
# $SERVICE_HOME/.archon instead of /root/.archon, and the build cannot read
# root's Codex credentials even by accident.
Environment=HOME=$SERVICE_HOME
Environment=USER=$SERVICE_USER
# \`uv\` is published to /usr/local/bin by this installer: the operator's copy
# in ~/.local/bin is not readable from a different account.
Environment=PATH=$SERVICE_PATH
Environment=BUILD_EXTRA_PATH=/usr/local/bin
# Deliberately no ProtectSystem/ProtectHome: see the header.

[Install]
# Without this, \`systemctl enable\` warns and the runner does not come back
# after a reboot — the queue would silently stop draining until someone noticed.
WantedBy=multi-user.target
UNIT

chmod 644 "$DIR/$UNIT.service"
systemctl daemon-reload

if $start; then
    systemctl enable "$UNIT.service"
    # restart, not `enable --now`: re-running this installer is how you apply a
    # change to the unit *or* to build-runner.py, and `enable --now` on an already
    # active service does nothing — leaving the new files on disk and the old code
    # in memory, which reads as the fix not working.
    systemctl restart "$UNIT.service"
    say "installed and (re)started: $UNIT.service -> scripts/build-runner.py --serve"
else
    say "installed: $UNIT.service (not started)"
fi

echo
say "environment the runner will use:"
# Run the check AS the service account. Running it as root would happily read a
# .env the builder cannot, which is the one failure this install is most likely
# to have — and it would look green right up until the first build.
if ! $run_as_root && command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" "USER=$SERVICE_USER" \
        PATH="$SERVICE_PATH" BUILD_EXTRA_PATH=/usr/local/bin \
        "$PYTHON" "$REPO_ROOT/scripts/build-runner.py" --check \
        || warn "the runner reported a problem above"
else
    "$PYTHON" "$REPO_ROOT/scripts/build-runner.py" --check || warn "the runner reported a problem above"
fi
echo
echo "  status:   systemctl status $UNIT"
echo "  logs:     journalctl -u $UNIT -f"
echo "  queue:    python3 scripts/build-runner.py --list"
echo "  queue one: python3 scripts/build-runner.py --submit build-requests/<name>.md"
