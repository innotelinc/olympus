#!/usr/bin/env bash
# install-token-check-timer.sh — schedule an alert wrapper as a systemd timer.
#
# Writes two units into /etc/systemd/system and enables the timer:
#
#   olympus-<target>-check.service   runs scripts/<target>-alert.sh
#   olympus-<target>-check.timer     fires it daily, and 2 min after boot
#
# Four targets exist:
#
#   studio-token   credential expiry check for `make studio-oidc`
#                  (scripts/studio-token-alert.sh)
#   vault-renew    daily renewal of the stack's periodic Vault token
#                  (scripts/vault-renew-alert.sh)
#   build-model    can the configured build model call a tool, twice over
#                  (scripts/build-model-alert.sh)
#   gateway-backup the gateway's connections + the key that decrypts them, into
#                  Vault (scripts/gateway-backup-alert.sh)
#
# gateway-backup is the one target here that writes something — into Vault, over
# the network, which is why it still fits this hardening: it reads the gateway's
# data dir and stores what it read, and writes nothing on this host.
#
# On expiry or failure the units land in `systemctl --failed`, which is the
# fallback signal when Telegram is not configured yet; the service is hardened
# (read-only filesystems, no new privileges) because all three wrappers are
# read-only by design. `build-model` is read-only in the same sense and one thing
# more: it makes a model request, and never executes what the model proposes.
#
# build-model runs on the same daily schedule as the others. Quota exhaustion is
# its usual finding and quota recovers, so the cost of a daily request is the
# price of being told the day a chain goes quiet rather than the day someone
# notices an empty app.
#
# The service's WorkingDirectory and command are substituted from THIS checkout,
# so re-running the installer after moving the repo re-points the units. The
# wrappers are idempotent and read .env themselves, so nothing else is needed.
#
#   scripts/install-token-check-timer.sh                          # all three
#   scripts/install-token-check-timer.sh TARGET=vault-renew       # one
#   scripts/install-token-check-timer.sh TARGET=build-model       # one
#   scripts/install-token-check-timer.sh TARGET=studio-token --uninstall
#   systemctl list-timers 'olympus-*-check.timer'
#   systemctl start olympus-studio-token-check.service   # run one now
#   journalctl -u olympus-studio-token-check -n 20
set -euo pipefail

DIR=/etc/systemd/system
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# $1 = target name; sets TARGET_SCRIPT and UNIT_BASE
resolve_target() {
    case "$1" in
        studio-token)   TARGET_SCRIPT=studio-token-alert.sh ;;
        vault-renew)    TARGET_SCRIPT=vault-renew-alert.sh ;;
        build-model)    TARGET_SCRIPT=build-model-alert.sh ;;
        gateway-backup) TARGET_SCRIPT=gateway-backup-alert.sh ;;
        *) return 1 ;;
    esac
    UNIT_BASE="olympus-$1-check"
}

uninstall() {
    local unit="$1"
    systemctl disable --now "$unit.timer" 2>/dev/null || true
    rm -f "$DIR/$unit.service" "$DIR/$unit.timer"
    systemctl reset-failed "$unit.service" 2>/dev/null || true
    echo "removed the $unit units"
}

targets=()
uninstall_only=false
for arg in "$@"; do
    case "$arg" in
        --uninstall) uninstall_only=true ;;
        TARGET=*) targets+=("${arg#TARGET=}") ;;
        *) echo "unknown argument: $arg (expected TARGET=<studio-token|vault-renew|build-model|gateway-backup> and/or --uninstall)" >&2; exit 2 ;;
    esac
done
if [[ ${#targets[@]} -eq 0 ]]; then targets=(studio-token vault-renew build-model gateway-backup); fi

for target in "${targets[@]}"; do
    resolve_target "$target" || { echo "unknown target: $target (expected studio-token, vault-renew, build-model or gateway-backup)" >&2; exit 2; }

    [[ -f "$REPO_ROOT/scripts/$TARGET_SCRIPT" ]] || {
        echo "scripts/$TARGET_SCRIPT is missing next to this installer" >&2
        exit 2
    }

    if $uninstall_only; then
        uninstall "$UNIT_BASE"
        continue
    fi

    [[ $(id -u) -eq 0 ]] || {
        echo "needs root to write $DIR and enable the timer (try sudo)" >&2
        exit 2
    }

    cat > "$DIR/$UNIT_BASE.service" <<UNIT
[Unit]
Description=Olympus $target — daily check/renewal with Telegram alerting
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
# The wrapper resolves the repo .env itself; the unit carries no credential
# and no host name.
WorkingDirectory=$REPO_ROOT
ExecStart=$REPO_ROOT/scripts/$TARGET_SCRIPT
# Read-only by design: if this ever starts failing with EROFS, something began
# writing, and that is a finding, not a regression.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
UNIT

    cat > "$DIR/$UNIT_BASE.timer" <<UNIT
[Unit]
Description=Daily Olympus $target check (alert before it becomes an outage)

[Timer]
# Daily at 06:17 — off-peak, and not on the hour where everything else lands.
OnCalendar=*-*-* 06:17:00
RandomizedDelaySec=10m
Persistent=yes
# Re-check shortly after boot: a host that was down while something lapsed
# should find out on the first boot back, not at 06:17 next day.
OnBootSec=2min

[Install]
WantedBy=timers.target
UNIT

    chmod 644 "$DIR/$UNIT_BASE.service" "$DIR/$UNIT_BASE.timer"
    systemctl daemon-reload
    systemctl enable --now "$UNIT_BASE.timer"

    echo "installed: $UNIT_BASE.timer -> scripts/$TARGET_SCRIPT"
done

if ! $uninstall_only; then
    echo
    systemctl list-timers 'olympus-*-check.timer' --no-pager | sed -n '1,3p'
    echo
    echo "run one right now:   systemctl start olympus-studio-token-check.service"
    echo "read the output:     journalctl -u olympus-studio-token-check -n 20"
    echo "test the channel:    $REPO_ROOT/scripts/studio-token-alert.sh --test-telegram"
    echo "                     $REPO_ROOT/scripts/vault-renew-alert.sh --test-telegram"
    echo "                     $REPO_ROOT/scripts/build-model-alert.sh --test-telegram"
    echo "                     $REPO_ROOT/scripts/gateway-backup-alert.sh --test-telegram"
fi
