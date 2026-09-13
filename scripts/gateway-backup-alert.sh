#!/usr/bin/env bash
# gateway-backup-alert.sh — back the gateway's state up, alert when it fails.
#
# Built for the systemd timer (scripts/install-token-check-timer.sh
# TARGET=gateway-backup) but fine from cron or by hand. It runs
# scripts/omniroute-vault-backup.py, which copies the two things a gateway cannot
# be rebuilt without into Cerulean Vault:
#
#   server.env      STORAGE_ENCRYPTION_KEY — what decrypts the connections
#   connections     every provider connection, decrypted
#
# Either half alone restores a gateway that does not work: the connections without
# the key are unreadable, and the key without the connections protects nothing. So
# the wrapper treats any non-zero exit as an outage-in-waiting:
#
#   1  the backup or the write to Vault failed — the copy is now behind
#   2  the checkout or the gateway is not in a state where this means anything
#      (no Vault address/token, or the gateway has no data dir yet)
#
# WHY A DAILY BACKUP RATHER THAN A DAILY DRIFT CHECK. Because it cannot drift:
# every run re-reads the live gateway and writes what it found. A check compares a
# copy against a moving target, which is one more thing to be wrong. The script's
# own `--check` mode is there for the times you want the comparison without a
# write — after a restore, or before tearing a host down.
#
# Delivery is Telegram (the platform's channel) via scripts/notify-telegram.sh:
# TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, from the environment or the repo .env.
# Until both are real it does not alert anywhere — it says so in the journal and
# keeps exiting non-zero, so `systemctl --failed` and the timer's state are the
# fallback signal. Delivery failure never changes the exit code.
#
# Alerts repeat on every run while the backup is failing, deliberately: a backup
# that stopped three weeks ago is only useful the day it is caught.
#
# Test the channel after pasting real credentials:
#
#   scripts/gateway-backup-alert.sh --test-telegram
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST="$(hostname)"
FIX_HINT="fix: make gateway-vault-backup — and read docs/stack.md if the gateway has no data dir yet"

say() { printf '%s gateway-backup-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Telegram caps a message at 4096 characters; this report is short, but a Vault
# error body is not, and a truncated alert beats a rejected one.
clip() {
    local text="$1"
    if [ "${#text}" -le 3000 ]; then
        printf '%s' "$text"
    else
        printf '%s\n… (truncated)' "${text:0:3000}"
    fi
}

alert() {
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: ${verdict} (exit ${rc})"
    say "$output"

    if telegram_configured; then
        if send_telegram "$(clip "$(printf '[%s] Gateway backup %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — journal entry is the only record"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
        say "configure them in .env, then test with: scripts/gateway-backup-alert.sh --test-telegram"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    if send_telegram "[${HOST}] test message from olympus-gateway-backup-alert — the gateway backup alert channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

# The script resolves .env itself, so --check / --json / --dry-run pass straight
# through. Its stdout is the report, which is what the alert carries.
output="$(python3 "$REPO_ROOT/scripts/omniroute-vault-backup.py" "$@" 2>&1)"
rc=$?

case "$rc" in
    0)
        say "backed up: $(printf '%s' "$output" | tr '\n' ' ')"
        exit 0
        ;;
    1)
        alert "FAILED — the stored copy is behind the gateway, so a rebuild would lose credentials" "$output" "$rc"
        ;;
    2)
        alert "not configured — nothing was stored (Vault address/token, or no data dir yet)" "$output" "$rc"
        ;;
    *)
        alert "wrapper broke (exit ${rc})" "$output" "$rc"
        ;;
esac
