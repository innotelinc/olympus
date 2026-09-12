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
# Delivery is Telegram (the platform's channel): TELEGRAM_BOT_TOKEN plus
# TELEGRAM_CHAT_ID, from the environment or the repo .env. Until both are real
# the script does not alert anywhere — it says so in the journal and keeps
# exiting non-zero, so `systemctl --failed` and the timer's state are the
# fallback signal. Delivery failure never changes the exit code: the check's
# verdict is the signal, not the notification.
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
HOST="$(hostname)"
FIX_HINT="fix: make studio-token-rotate AUTHENTIK_HOST=<host running cerulean-authentik>"

say() { printf '%s studio-token-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

env_value() {
    # Last assignment wins, matching how the Python tooling reads the file.
    local value
    value="$(grep -E "^$1=" "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\"' | tr -d "'")"
    printf '%s' "$value"
}

telegram_configured() {
    local token chat
    token="${TELEGRAM_BOT_TOKEN:-$(env_value TELEGRAM_BOT_TOKEN)}"
    chat="${TELEGRAM_CHAT_ID:-$(env_value TELEGRAM_CHAT_ID)}"
    [[ -n "$token" && -n "$chat" ]] || return 1
    case "$token" in *change-me*|*change_me*|*PASTE_YOUR*) return 1 ;; esac
    case "$chat" in *PASTE_YOUR*|*change-me*) return 1 ;; esac
    return 0
}

send_telegram() {
    # $1 = message. Uses python for the JSON so arbitrary text is safe, and
    # honours TELEGRAM_API_BASE so the send path can be tested against a local
    # receiver without touching the real bot.
    local base token chat
    base="${TELEGRAM_API_BASE:-https://api.telegram.org}"
    token="${TELEGRAM_BOT_TOKEN:-$(env_value TELEGRAM_BOT_TOKEN)}"
    chat="${TELEGRAM_CHAT_ID:-$(env_value TELEGRAM_CHAT_ID)}"

    ALERT_TEXT="$1" ALERT_BASE="$base" ALERT_TOKEN="$token" ALERT_CHAT="$chat" python3 - <<'PY'
import json
import os
import sys
import urllib.request

text = os.environ["ALERT_TEXT"]
base = os.environ["ALERT_BASE"].rstrip("/")
payload = json.dumps(
    {"chat_id": os.environ["ALERT_CHAT"], "text": text, "disable_web_page_preview": True}
).encode()
request = urllib.request.Request(
    f"{base}/bot{os.environ['ALERT_TOKEN']}/sendMessage",
    data=payload,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read() or b"{}")
        if not body.get("ok"):
            sys.exit(f"telegram answered not-ok: {json.dumps(body)[:200]}")
except urllib.error.HTTPError as error:
    sys.exit(f"telegram send failed: HTTP {error.code} — {error.read().decode(errors='replace')[:200]}")
except (urllib.error.URLError, OSError) as error:
    sys.exit(f"telegram send failed: {getattr(error, 'reason', error)}")
PY
}

alert() {
    # $1 = verdict, $2 = check output, $3 = exit code to preserve
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: registration credential ${verdict} (exit ${rc})"
    say "$output"

    if telegram_configured; then
        if send_telegram "$(printf '[%s] Studio registration credential %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")"; then
            say "alerted: Telegram chat ${TELEGRAM_CHAT_ID:-$(env_value TELEGRAM_CHAT_ID)}"
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
