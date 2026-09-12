#!/usr/bin/env bash
# install-token-check-timer.sh — schedule the credential check as a systemd timer.
#
# Writes two units into /etc/systemd/system and enables the timer:
#
#   olympus-studio-token-check.service   runs scripts/studio-token-alert.sh
#   olympus-studio-token-check.timer     fires it daily, and 2 min after boot
#
# On expiry the units land in `systemctl --failed`, which is the fallback signal
# when Telegram is not configured yet; the service is hardened (read-only
# filesystems, no new privileges) because the check is read-only by design.
#
# The service's WorkingDirectory and command are substituted from THIS checkout,
# so re-running the installer after moving the repo re-points the units. The
# check itself is idempotent and reads .env itself, so nothing else is needed.
#
#   scripts/install-token-check-timer.sh            # install + enable + start
#   scripts/install-token-check-timer.sh --uninstall
#   systemctl list-timers olympus-studio-token-check.timer
#   systemctl start olympus-studio-token-check.service   # run it now
#   journalctl -u olympus-studio-token-check -n 20
set -euo pipefail

UNIT=olympus-studio-token-check
DIR=/etc/systemd/system
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

[[ -f "$REPO_ROOT/scripts/studio-token-alert.sh" ]] || {
    echo "scripts/studio-token-alert.sh is missing next to this installer" >&2
    exit 2
}
[[ $(id -u) -eq 0 ]] || {
    echo "needs root to write $DIR and enable the timer (try sudo)" >&2
    exit 2
}

if [[ "${1:-}" == "--uninstall" ]]; then
    systemctl disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "$DIR/$UNIT.service" "$DIR/$UNIT.timer"
    systemctl daemon-reload
    systemctl reset-failed "$UNIT.service" 2>/dev/null || true
    echo "removed the $UNIT units"
    exit 0
fi

cat > "$DIR/$UNIT.service" <<UNIT
[Unit]
Description=Olympus Studio registration credential — expiry check (alerts via Telegram)
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
# The check resolves the repo .env itself; the alert script only needs the repo
# root, so the unit carries no credential and no host name.
WorkingDirectory=$REPO_ROOT
ExecStart=$REPO_ROOT/scripts/studio-token-alert.sh
# Read-only by design: if this ever starts failing with EROFS, something began
# writing, and that is a finding, not a regression.
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
UNIT

cat > "$DIR/$UNIT.timer" <<UNIT
[Unit]
Description=Check the Studio registration credential daily (alert before it lapses)

[Timer]
# Daily at 06:17 — off-peak, and not on the hour where everything else lands.
OnCalendar=*-*-* 06:17:00
RandomizedDelaySec=10m
Persistent=yes
# Re-check shortly after boot: a host that was down while the credential
# expired should find out on the first boot back, not at 06:17 next day.
OnBootSec=2min

[Install]
WantedBy=timers.target
UNIT

chmod 644 "$DIR/$UNIT.service" "$DIR/$UNIT.timer"
systemctl daemon-reload
systemctl enable --now "$UNIT.timer"

echo "installed:"
echo "  $DIR/$UNIT.service"
echo "  $DIR/$UNIT.timer"
systemctl list-timers "$UNIT.timer" --no-pager | sed -n '1,2p'
echo
echo "next runs are visible with: systemctl list-timers $UNIT.timer"
echo "run it right now:           systemctl start $UNIT.service"
echo "read the output:            journalctl -u $UNIT -n 20"
echo "test the Telegram channel:  $REPO_ROOT/scripts/studio-token-alert.sh --test-telegram"
