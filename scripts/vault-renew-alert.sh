#!/usr/bin/env bash
# vault-renew-alert.sh — keep the stack's Vault token alive, alert when it cannot be.
#
# Built for the systemd timer (scripts/install-token-check-timer.sh) but fine
# from cron or by hand. Wraps scripts/vault-renew.sh:
#
#   default    renew the periodic token (every renewal resets its TTL to the
#              full 32-day period, so a daily run keeps it alive indefinitely)
#   --check    report without renewing
#
# Any failure means this stack is heading for a locked vault — every secret the
# stack reads, including the Studio registration credential, resolves through
# that token — so the failure is alerted the same way the credential check
# alerts: Telegram when configured, the journal either way. The output of the
# underlying script is never paraphrased; it is the diagnostic.
#
# Unlike the credential check this is not expected to alert routinely: a healthy
# run renews silently and exits 0.
#
# Test the channel after pasting real credentials:
#
#   scripts/vault-renew-alert.sh --test-telegram
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST="$(hostname)"
FIX_HINT="fix: re-mint the token on the Vault host (vault token create -orphan -policy=olympus -period=768h) — see docs/stack.md, Secrets"

say() { printf '%s vault-renew-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

alert() {
    # $1 = verdict, $2 = renew output, $3 = exit code to preserve
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: the stack's Vault token ${verdict} (exit ${rc})"
    say "$output"

    if telegram_configured; then
        if send_telegram "$(printf '[%s] Olympus Vault token %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — journal entry is the only record; fix the channel or the token"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
        say "configure them in .env, then test with: scripts/vault-renew-alert.sh --test-telegram"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    if send_telegram "[${HOST}] test message from olympus-vault-renew-alert — the Vault token alert channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

mode="renew"
[[ "${1:-}" == "--check" ]] && mode="check"

output="$(bash "$REPO_ROOT/scripts/vault-renew.sh" "$@" 2>&1)"
rc=$?

case "$rc" in
    0)
        say "$mode ok: $output"
        exit 0
        ;;
    *)
        alert "could not be ${mode}d — secrets resolve through it, so the stack is heading for a lockout" "$output" "$rc"
        ;;
esac
