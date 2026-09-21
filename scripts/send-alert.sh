#!/bin/bash
# Send an alert email via local sendmail/postfix.
#
# launchd kills a job's process group when the main process exits
# (ExitTimeOut defaults to a few seconds). `mail` returns as soon as
# postfix has *queued* the message, and the SMTP relay to Gmail then
# runs in child processes -- which launchd would SIGKILL. This helper
# invokes sendmail in the foreground and logs the attempt; the launchd
# plists also set AbandonProcessGroup so any leftover postfix workers
# can finish.
#
# Usage:
#   echo "body" | send-alert.sh "subject"
#   send-alert.sh --test
set -euo pipefail

DATA_DIR="${HOME}/.loseit-data"
LOG_DIR="${DATA_DIR}/logs"
ALERT_LOG="${LOG_DIR}/alert.log"
ALERT_EMAIL="${LOSEIT_ALERT_EMAIL:-}"
SENDMAIL="${LOSEIT_SENDMAIL:-/usr/sbin/sendmail}"

mkdir -p "$LOG_DIR"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >>"$ALERT_LOG"
}

if [[ "${1:-}" == "--test" ]]; then
  SUBJECT="loseit-scraper TEST alert"
  BODY="This is a test alert from loseit-mcp-local at $(date '+%Y-%m-%d %H:%M:%S %Z').
If you received this, scheduled failure/stale emails can be delivered."
else
  SUBJECT="${1:?usage: send-alert.sh <subject>  (body on stdin)}"
  BODY="$(cat)"
fi

if [[ -z "$ALERT_EMAIL" ]]; then
  log "SKIP subject=${SUBJECT} (LOSEIT_ALERT_EMAIL unset)"
  echo "WARNING: LOSEIT_ALERT_EMAIL unset; alert not sent: ${SUBJECT}" >&2
  exit 1
fi

ERR=$(mktemp -t loseit-alert.XXXXXX)
trap 'rm -f "$ERR"' EXIT

set +e
"$SENDMAIL" -i -f "$ALERT_EMAIL" -- "$ALERT_EMAIL" >"$ERR" 2>&1 <<EOF
From: ${ALERT_EMAIL}
To: ${ALERT_EMAIL}
Subject: ${SUBJECT}
MIME-Version: 1.0
Content-Type: text/plain; charset=UTF-8

${BODY}
EOF
STATUS=$?
set -e

ERR_FLAT=$(tr '\n' ' ' <"$ERR" | sed 's/[[:space:]]*$//')
if [[ "$STATUS" -ne 0 ]]; then
  log "FAIL exit=${STATUS} to=${ALERT_EMAIL} subject=${SUBJECT} err=${ERR_FLAT}"
  echo "WARNING: alert send failed (exit ${STATUS}) to ${ALERT_EMAIL}" >&2
  cat "$ERR" >&2
  exit "$STATUS"
fi

log "SENT exit=0 to=${ALERT_EMAIL} subject=${SUBJECT}"
exit 0
