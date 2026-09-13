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
#   scripts/install-build-runner.sh              # install and start
#   scripts/install-build-runner.sh --uninstall
#   scripts/install-build-runner.sh --no-start   # write the unit only
#   systemctl status olympus-build-runner
#   journalctl -u olympus-build-runner -f
#   python3 scripts/build-runner.py --list
set -euo pipefail

DIR=/etc/systemd/system
UNIT=olympus-build-runner
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"

say()  { printf '\033[1;32m==> \033[0m%s\n' "$*"; }
warn() { printf '\033[1;33m==> \033[0m%s\n' "$*" >&2; }
die()  { printf '\033[1;31m==> \033[0m%s\n' "$*" >&2; exit 1; }

uninstall=false
start=true
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall=true ;;
        --no-start)  start=false ;;
        *) die "unknown argument: $arg (expected --uninstall and/or --no-start)" ;;
    esac
done

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

cat > "$DIR/$UNIT.service" <<UNIT
[Unit]
Description=Olympus build runner — runs \`make app\` for builds queued by Studio
# The gateway is a container on this compose project; waiting for docker keeps a
# boot-time queue from starting against an endpoint that is not listening yet.
Wants=network-online.target
After=network-online.target docker.service

[Service]
Type=simple
WorkingDirectory=$REPO_ROOT
# The runner reads the repo .env itself, allow-listing OMNIROUTE_*/ARCHON_* — so
# unlike EnvironmentFile= it never hands the build the Vault or Authentik tokens.
# The unit therefore carries no credential and no host name.
ExecStart=$PYTHON $REPO_ROOT/scripts/build-runner.py --serve
Restart=always
RestartSec=5
# uv lives in ~/.local/bin and the workflow's script nodes declare \`runtime: uv\`;
# the runner prepends it too, but the unit should not depend on that.
Environment=PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=HOME=/root
# Deliberately no ProtectSystem/ProtectHome: see the header.

[Install]
# Without this, `systemctl enable` warns and the runner does not come back after
# a reboot — the queue would silently stop draining until someone noticed.
WantedBy=multi-user.target
UNIT

chmod 644 "$DIR/$UNIT.service"
systemctl daemon-reload

if $start; then
    systemctl enable --now "$UNIT.service"
    say "installed and started: $UNIT.service -> scripts/build-runner.py --serve"
else
    say "installed: $UNIT.service (not started)"
fi

echo
say "environment the runner will use:"
"$PYTHON" "$REPO_ROOT/scripts/build-runner.py" --check || warn "the runner reported a problem above"
echo
echo "  status:   systemctl status $UNIT"
echo "  logs:     journalctl -u $UNIT -f"
echo "  queue:    python3 scripts/build-runner.py --list"
echo "  queue one: python3 scripts/build-runner.py --submit build-requests/<name>.md"
