#!/usr/bin/env bash
# gateway-edge-alert.sh — is the gateway's public name reachable? Alert when not.
#
# Built for the systemd timer (scripts/install-token-check-timer.sh
# TARGET=gateway-edge) but fine from cron or by hand. It runs
# scripts/gateway-edge-check.py, which walks the name link by link — DNS, TLS,
# edge, SSO proxy — and stops at the first one that is broken.
#
# WHY THIS CHECKS THE PATH AND NOT THE HOST. "It's not resolving" is what gets
# reported and it is almost never what happened: a dead edge, a lapsed
# certificate and a stopped proxy all look the same in a browser. The check's
# output is the diagnosis, and this wrapper's job is to carry that diagnosis to
# someone, because the difference between "renew the certificate" and "the
# platform host is down" is the whole value of running it.
#
# Exit codes from the check:
#   0  every link answered (a resolver that did not answer while another did is
#      reported as a warning, not a failure — the name is reachable)
#   1  a link is broken; the verdict names which
#   2  the check could not run at all
#
# Delivery is Telegram (the platform's channel) via scripts/notify-telegram.sh:
# TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, from the environment or the repo .env.
# Until both are real it does not alert anywhere — it says so in the journal and
# keeps exiting non-zero, so `systemctl --failed` and the timer's state are the
# fallback signal. Delivery failure never changes the exit code.
#
# Alerts repeat on every run while the name is down, deliberately: an outage
# nobody hears about is the one that lasts the longest.
#
# Test the channel after pasting real credentials:
#
#   scripts/gateway-edge-alert.sh --test-telegram
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST_NAME="$(hostname)"
FIX_HINT="fix: read the link that FAILED above. dns → make gateway-edge · proxy → make gateway-sso-up · certificate → the cert has lapsed, reissue it in Cerulean"

say() { printf '%s gateway-edge-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Telegram caps a message at 4096 characters. The check's report is well under
# that, but a resolver error string is not bounded, and a truncated alert beats
# a rejected one.
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
    printf '%s\n' "$output" | while IFS= read -r line; do say "$line"; done

    if telegram_configured; then
        if send_telegram "$(clip "$(printf '[%s] Gateway name %s\n\n%s\n\n%s' "$HOST_NAME" "$verdict" "$output" "$FIX_HINT")")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — journal entry is the only record"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
        say "configure them in .env, then test with: scripts/gateway-edge-alert.sh --test-telegram"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    if send_telegram "[${HOST_NAME}] test message from olympus-gateway-edge-check — the gateway name alert channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

# The check resolves .env itself, so --host / --json / --no-sso pass straight
# through. Its stdout is the report, which is what the alert carries.
output="$(python3 "$REPO_ROOT/scripts/gateway-edge-check.py" "$@" 2>&1)"
rc=$?

case "$rc" in
    0)
        # Even a pass can carry a warning (a resolver that did not answer). It goes
        # to the journal so the precedent is on record before it becomes an outage.
        printf '%s\n' "$output" | while IFS= read -r line; do say "$line"; done
        exit 0
        ;;
    1)
        alert "UNREACHABLE — $(printf '%s' "$output" | tail -1)" "$output" "$rc"
        ;;
    2)
        alert "not configured — nothing was checked" "$output" "$rc"
        ;;
    *)
        alert "wrapper broke (exit ${rc})" "$output" "$rc"
        ;;
esac
