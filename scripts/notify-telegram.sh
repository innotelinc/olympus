#!/usr/bin/env bash
# notify-telegram.sh — send a Telegram message using this stack's credentials.
#
# Sourced, not executed. Callers get two functions:
#
#   telegram_configured   -> 0 when both credentials are real, 1 otherwise
#   send_telegram TEXT    -> 0 when Telegram accepted the message, 1 on failure
#
# Credentials come from the process env first, then the repo .env (last
# assignment wins, matching the Python tooling). A placeholder — the shape
# .env.example ships — counts as unset wherever it is found, so a `change-me`
# export cannot shadow a real value in the file: process env beats .env for
# every reader here too, and that rule is what makes the beat honest.
#
# Requires REPO_ROOT to be set by the caller (the repo checkout holding .env).
# TELEGRAM_API_BASE overrides the API endpoint for testing; the production
# default is https://api.telegram.org. Sending only, never polling — an alert
# here shares a bot with an interactive listener without either fighting over
# getUpdates.

: "${REPO_ROOT:?notify-telegram.sh is sourced; set REPO_ROOT first}"

env_value() {
    local value
    value="$(grep -E "^$1=" "$REPO_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\"' | tr -d "'")"
    printf '%s' "$value"
}

is_placeholder() {
    local lowered="${1,,}"
    case "$lowered" in
        *change-me*|*change_me*|*changeme*|*paste_your*|*placeholder*|*your-*|*xxx*) return 0 ;;
        *) return 1 ;;
    esac
}

credential_value() {
    # $1 = variable name. Process env wins over .env — unless it is empty or a
    # placeholder, in which case the file is consulted. A real export wins over
    # a stale file; a placeholder export loses to a real file value.
    local env_val="${!1:-}"
    if [[ -n "$env_val" ]] && ! is_placeholder "$env_val"; then
        printf '%s' "$env_val"
        return
    fi
    local file_val
    file_val="$(env_value "$1")"
    if [[ -n "$file_val" ]] && ! is_placeholder "$file_val"; then
        printf '%s' "$file_val"
    fi
}

telegram_configured() {
    [[ -n "$(credential_value TELEGRAM_BOT_TOKEN)" && -n "$(credential_value TELEGRAM_CHAT_ID)" ]]
}

send_telegram() {
    # $1 = message text. python builds the JSON so arbitrary text is safe.
    local base token chat
    base="${TELEGRAM_API_BASE:-https://api.telegram.org}"
    token="$(credential_value TELEGRAM_BOT_TOKEN)"
    chat="$(credential_value TELEGRAM_CHAT_ID)"

    ALERT_TEXT="$1" ALERT_BASE="$base" ALERT_TOKEN="$token" ALERT_CHAT="$chat" python3 - <<'PY'
import json
import os
import sys
import urllib.error
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
