#!/bin/bash
# launchd entrypoint for the daily LoseIt scrape.
# On non-zero exit: email the captured output (if LOSEIT_ALERT_EMAIL is set).
# On success: write ~/.loseit-data/last_success for the health check.
set -euo pipefail

# Prefer LOSEIT_PROJECT (set by install-launchd / launchd) so the job can
# run from a copy outside ~/Documents (macOS TCC blocks launchd from
# executing scripts inside Documents). Interactive runs fall back to the
# repo layout (this file lives in scripts/).
if [[ -n "${LOSEIT_PROJECT:-}" ]]; then
  PROJECT="$LOSEIT_PROJECT"
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PROJECT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi
DATA_DIR="${HOME}/.loseit-data"
LOG_DIR="${DATA_DIR}/logs"
MARKER="${DATA_DIR}/last_success"
ALERT_EMAIL="${LOSEIT_ALERT_EMAIL:-}"

# launchd gives a minimal PATH; ensure mail/date/etc. are findable.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

mkdir -p "$LOG_DIR" "$DATA_DIR"

OUT=$(mktemp -t loseit-scraper.XXXXXX)
trap 'rm -f "$OUT"' EXIT

set +e
"${PROJECT}/.venv/bin/python" "${PROJECT}/src/scraper.py" run >"$OUT" 2>&1
STATUS=$?
set -e

# Append a dated copy of this run's output for longer-lived history
# (launchd's StandardOut/Err paths also capture anything we print below).
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') exit=${STATUS} ====="
  cat "$OUT"
  echo
} >>"${LOG_DIR}/scraper.log"

if [[ "$STATUS" -ne 0 ]]; then
  BODY=$(
    {
      echo "loseit-scraper FAILED at $(date '+%Y-%m-%d %H:%M:%S %Z')"
      echo "host: $(hostname)"
      echo "exit: ${STATUS}"
      echo "----"
      cat "$OUT"
    }
  )
  if [[ -n "$ALERT_EMAIL" ]]; then
    echo "$BODY" | mail -s "loseit-scraper FAILED" "$ALERT_EMAIL" || true
  else
    echo "WARNING: scrape failed (exit ${STATUS}); set LOSEIT_ALERT_EMAIL to enable email alerts" >&2
  fi
  # Also echo so launchd's StandardErrorPath captures it.
  cat "$OUT" >&2
  exit "$STATUS"
fi

date -u +%Y-%m-%dT%H:%M:%SZ >"$MARKER"
cat "$OUT"
exit 0
