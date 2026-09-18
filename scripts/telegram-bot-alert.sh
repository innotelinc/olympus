#!/usr/bin/env bash
# telegram-bot-alert.sh — is this stack's Telegram bot token still valid?
#
# The token in `.env` is what the interactive Telegram front end (the Hermes
# gateway service) and every alert wrapper share. It can go stale without
# anything in this repo changing — a token regenerated in @BotFather, a bot
# deleted, a value rotated on the gateway and not here — and the failure mode is
# quiet: alerts stop arriving and the front end never connects, while every
# script that reads the file still looks fine.
#
# This is the check that fails loudly instead. It calls Telegram's getMe, which
# is the one endpoint that answers "is this token real, and for which bot" and
# costs nothing:
#
#   * 200                  the token is live; the bot's @username and id are printed
#   * 401                  the token is revoked, deleted or mistyped
#   * network failure      Telegram is unreachable from here (not a token verdict)
#
# It does NOT poll getUpdates — the interactive gateway owns that, and two
# pollers on one bot cancel each other out.
#
# Delivery is Telegram via scripts/notify-telegram.sh, on the same rule as the
# other wrappers: when both credentials are real it alerts, otherwise it says so
# in the journal and keeps exiting non-zero so `systemctl --failed` is the
# fallback signal. A 401 is the one verdict it cannot alert on — the credentials
# it would use to send are the ones that just failed — so for that case the
# journal and the failed unit are the whole signal, deliberately.
#
#   scripts/telegram-bot-alert.sh
#   scripts/telegram-bot-alert.sh --test-telegram   # also proves the channel
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=notify-telegram.sh
source "$REPO_ROOT/scripts/notify-telegram.sh"
HOST="$(hostname)"

say() { printf '%s telegram-bot-alert: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

FIX_HINT="fix: create a bot with @BotFather (/newbot), paste the token into TELEGRAM_BOT_TOKEN in .env, and update the interactive gateway's own copy so both use the same bot"

# getMe via python (the same JSON-over-urllib shape notify-telegram.sh uses, so
# neither needs curl or jq). The verdict goes to STDOUT as "OK <username> <id>"
# or "FAIL <reason>" and the exit code mirrors it — the caller captures the
# stdout, so a verdict written to stderr would read as "no verdict" and be
# alerted on. Diagnostics belong on stderr, the verdict on stdout.
check_token() {
    local base token
    base="${TELEGRAM_API_BASE:-https://api.telegram.org}"
    token="$(credential_value TELEGRAM_BOT_TOKEN)"
    ALERT_BASE="$base" ALERT_TOKEN="$token" python3 - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

base = os.environ["ALERT_BASE"].rstrip("/")
token = os.environ["ALERT_TOKEN"]
request = urllib.request.Request(f"{base}/bot{token}/getMe")
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read() or b"{}")
except urllib.error.HTTPError as error:
    detail = error.read().decode(errors="replace")[:200]
    print(f"FAIL HTTP {error.code} — {detail}")
    sys.exit(1)
except (urllib.error.URLError, OSError) as error:
    print(f"FAIL unreachable — {getattr(error, 'reason', error)}")
    sys.exit(1)

if not body.get("ok"):
    print(f"FAIL not-ok — {json.dumps(body)[:200]}")
    sys.exit(1)
result = body.get("result", {})
print(f"OK {result.get('username', '?')} {result.get('id', '?')}")
PY
}

alert() {
    local verdict="$1" output="$2" rc="$3"
    say "ALERT: ${verdict} (exit ${rc})"
    say "$output"
    if telegram_configured; then
        if send_telegram "$(printf '[%s] Telegram bot token %s\n\n%s\n\n%s' "$HOST" "$verdict" "$output" "$FIX_HINT")"; then
            say "alerted: Telegram chat …$(credential_value TELEGRAM_CHAT_ID | tail -c 4)"
        else
            say "telegram delivery FAILED — the token that would send this is the one that failed; journal is the record"
        fi
    else
        say "telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — journal entry only"
    fi
    return "$rc"
}

if [[ "${1:-}" == "--test-telegram" ]]; then
    if ! telegram_configured; then
        say "cannot test: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are missing or still placeholders"
        exit 2
    fi
    verdict="$(check_token 2>/dev/null || true)"
    say "getMe: $verdict"
    if [[ "$verdict" != OK* ]]; then
        say "the token itself is the problem — fix it before expecting any alert through it"
        exit 1
    fi
    if send_telegram "[${HOST}] test message from olympus-telegram-bot-alert — the token is valid and the channel works"; then
        say "test message sent — check the chat"
        exit 0
    fi
    say "test message FAILED — see the error above"
    exit 1
fi

if ! telegram_configured; then
    say "not configured — set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"
    exit 2
fi

verdict="$(check_token 2>/dev/null || true)"
case "$verdict" in
    OK*)
        say "token is valid: ${verdict#OK }"
        exit 0
        ;;
    "FAIL unreachable"*)
        # A network verdict, not a token verdict — never claim the token is bad.
        alert "could not be checked — Telegram was unreachable from this host" "$verdict" 1
        ;;
    *)
        alert "is no longer valid — the interactive gateway and every alert share it" "$verdict" 1
        ;;
esac
