#!/usr/bin/env bash
# build-model-alert.sh — run the build-model check, alert when it fails.
#
# Built for the systemd timer (scripts/install-token-check-timer.sh
# TARGET=build-model) but fine from cron or by hand. Anything non-zero from
# `build-model-check.py` means app builds are about to do nothing useful:
#
#   1  no model in the configured chain can call a tool — a build would run,
#      exit 0, and write nothing to disk. This is the expensive one: there is no
#      error to read anywhere, so it is discovered by noticing an empty app.
#   2  the check could not tell — config missing, or the gateway did not answer
#      at all. Not the same claim as 1, and it says so in the message.
#   other  the check itself broke.
#
# The check costs one or two model requests per run, which is why the timer is
# daily rather than hourly. Delivery is Telegram (the platform's channel) via
# scripts/notify-telegram.sh: TELEGRAM_BOT_TOKEN plus TELEGRAM_CHAT_ID, from the
# environment or the repo .env. Until both are real the script does not alert
# anywhere — it says so in the journal and keeps exiting non-zero, so
# `systemctl --failed` and the timer's state are the fallback signal. Delivery
# failure never changes the exit code: the check's verdict is the signal.
#
# Alerts repeat on every run while the chain is broken. That is deliberate — a
# build model is not something to be told about once.
#
# Test the channel after pasting real credentials:
#
#   scripts/build-model-alert.sh --test-telegram
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST="$(hostname)"
FIX_HINT="fix: check the provider's quota, not the model name — docs/build-model.md"

say() { printf '%s build-model-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# Telegram caps a message at 4096 characters; the check's report is short but a
# gateway error body is not, and a truncated alert beats a rejected one.
clip() {
    local text="$1"
    if [ "${#text}" -le 3000 ]; then
        printf '%s' "$text"
    else
        printf '%s\n… (truncated)' "${text:0:3000}"
    fi
}

alert() {
    # $1 = verdict, $2 = check output, $3 = exit code to preserve
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: ${verdict} (exit ${rc})"
    say "$output"

    if telegram_configured; then
        if send_telegram "$(clip "$(printf '[%s] Build model %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — journal entry is the only record; fix the channel or the model"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
        say "configure them in .env, then test with: scripts/build-model-alert.sh --test-telegram"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    if send_telegram "[${HOST}] test message from olympus-build-model-alert — the build-model alert channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

# The check resolves .env itself, so any flags (--model, --timeout, --json) pass
# straight through. Its stdout is the report, which is what the alert carries.
output="$(python3 "$REPO_ROOT/scripts/build-model-check.py" "$@" 2>&1)"
rc=$?

case "$rc" in
    0)
        say "build model ok: $(printf '%s' "$output" | tr '\n' ' ')"
        exit 0
        ;;
    1)
        alert "cannot build: no model in the configured chain calls a tool — builds would exit 0 and write nothing" "$output" "$rc"
        ;;
    2)
        alert "unverified: the check could not run (config or gateway)" "$output" "$rc"
        ;;
    *)
        alert "check broke (exit ${rc}) — treat the build model as unverified" "$output" "$rc"
        ;;
esac
