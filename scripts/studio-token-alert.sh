#!/usr/bin/env bash
# studio-token-alert.sh — run the credential expiry check, alert when it lapses.
#
# Built for the systemd timer (scripts/install-token-check-timer.sh) but fine
# from cron or by hand. Anything non-zero from
# `authentik-studio-token.py --check` means the registration credential needs
# attention:
#
#   2  lapsing — inside --warn-days (default 14) of expiry, or never expires
#   1  unusable — expired, revoked, missing, or the check could not reach
#                 Authentik or Vault at all
#   other      — the check itself broke
#
# Delivery is Telegram (the platform's channel) via scripts/notify-telegram.sh:
# TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, from the environment or the repo
# .env. Until both are real the script does not alert anywhere — it says so in
# the journal and keeps exiting non-zero, so `systemctl --failed` and the
# timer's state are the fallback signal. Delivery failure never changes the
# exit code: the check's verdict is the signal, not the notification.
#
# Alerts repeat on every run while the credential is lapsing. That is the point
# — a credential gating registration should nag daily for the last two weeks,
# not risk one missed message meaning silence until it expires.
#
# Test the channel after pasting real credentials:
#
#   scripts/studio-token-alert.sh --test-telegram
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST="$(hostname)"
FIX_HINT="fix: make studio-token-rotate AUTHENTIK_HOST=<host running cerulean-authentik>"

say() { printf '%s studio-token-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

alert() {
    # $1 = verdict, $2 = check output, $3 = exit code to preserve
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: registration credential ${verdict} (exit ${rc})"
    say "$output"

    if telegram_configured; then
        if send_telegram "$(printf '[%s] Studio registration credential %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — journal entry is the only record; fix the channel or the credential"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
        say "configure them in .env, then test with: scripts/studio-token-alert.sh --test-telegram"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    if send_telegram "[${HOST}] test message from olympus-studio-token-alert — the credential alert channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

# The check resolves .env itself, so any flags (--warn-days, --env-file,
# --api-base) pass straight through.
output="$(python3 "$REPO_ROOT/scripts/authentik-studio-token.py" --check "$@" 2>&1)"
rc=$?

# An exported AUTHENTIK_TOKEN that is a vault:// reference without its #key
# fragment is malformed by definition, and the process env beats .env — so a
# caller with such an export would blind the check even though the file is
# fine. Retry once from the file in that case; a real export still wins.
if [[ $rc -eq 1 && -n "${AUTHENTIK_TOKEN:-}" \
      && "${AUTHENTIK_TOKEN:-}" == vault://* && "${AUTHENTIK_TOKEN:-}" != *#* ]]; then
    say "exported AUTHENTIK_TOKEN is a vault:// reference missing #key — retrying from .env"
    output="$(env -u AUTHENTIK_TOKEN -u AUTHENTIK_URL \
        python3 "$REPO_ROOT/scripts/authentik-studio-token.py" --check "$@" 2>&1)"
    rc=$?
fi

case "$rc" in
    0)
        say "credential ok: $output"
        exit 0
        ;;
    2)
        alert "is lapsing — inside its warning window (or never expires)" "$output" "$rc"
        ;;
    1)
        alert "is unusable — expired, revoked, or the check could not reach Authentik/Vault" "$output" "$rc"
        ;;
    *)
        alert "check broke (exit ${rc}) — treat the credential as unverified" "$output" "$rc"
        ;;
esac
