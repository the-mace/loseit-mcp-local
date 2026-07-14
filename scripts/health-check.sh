#!/bin/bash
# Detect a missed or long-failed scrape without external heartbeats.
# Emails only when stale (if LOSEIT_ALERT_EMAIL is set), and at most once
# per calendar day while stale.
set -euo pipefail

DATA_DIR="${HOME}/.loseit-data"
MARKER="${DATA_DIR}/last_success"
DB="${DATA_DIR}/loseit.db"
STALE_ALERT_STAMP="${DATA_DIR}/last_stale_alert"
ALERT_EMAIL="${LOSEIT_ALERT_EMAIL:-}"
# 36h: covers a 06:00 run + next-day noon check, with headroom for sleep/travel.
MAX_AGE_HOURS="${LOSEIT_STALE_HOURS:-36}"
# When no success marker exists yet, treat DB max(date) older than this many
# days (relative to today) as stale. Scrapes target through yesterday, so 2
# means "no data for yesterday or today".
DB_MAX_LAG_DAYS="${LOSEIT_DB_STALE_DAYS:-2}"

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

mkdir -p "$DATA_DIR"

REASON=""
NOW=$(date +%s)

if [[ -f "$MARKER" ]]; then
  MTIME=$(stat -f %m "$MARKER")
  AGE=$(( NOW - MTIME ))
  MAX_AGE=$(( MAX_AGE_HOURS * 3600 ))
  if (( AGE > MAX_AGE )); then
    REASON="last_success is older than ${MAX_AGE_HOURS}h
marker content: $(cat "$MARKER")
marker mtime:   $(stat -f %Sm "$MARKER")"
  fi
else
  # No marker yet (first deploy, or never succeeded under the wrapper).
  # Fall back to SQLite freshness so we don't false-alarm right after install
  # if the scraper was already writing data via the old bare launchd job.
  if [[ -f "$DB" ]]; then
    LATEST=$(sqlite3 "$DB" "SELECT date FROM daily_summary ORDER BY date DESC LIMIT 1;" 2>/dev/null || true)
    if [[ -n "${LATEST:-}" ]]; then
      # macOS date: -j -f parse, +%s epoch
      LATEST_EPOCH=$(date -j -f "%Y-%m-%d" "$LATEST" +%s 2>/dev/null || echo 0)
      CUTOFF_EPOCH=$(date -v-"${DB_MAX_LAG_DAYS}d" -j -f "%Y-%m-%d" "$(date +%Y-%m-%d)" +%s)
      if (( LATEST_EPOCH < CUTOFF_EPOCH )); then
        REASON="no last_success marker, and daily_summary max date is ${LATEST} (older than ${DB_MAX_LAG_DAYS} days)"
      fi
    else
      REASON="no last_success marker, and daily_summary is empty or unreadable"
    fi
  else
    REASON="no last_success marker and no database at ${DB}"
  fi
fi

if [[ -z "$REASON" ]]; then
  exit 0
fi

# Dedup: one STALE mail per calendar day while still stale.
TODAY=$(date +%Y-%m-%d)
if [[ -f "$STALE_ALERT_STAMP" ]] && [[ "$(cat "$STALE_ALERT_STAMP")" == "$TODAY" ]]; then
  exit 0
fi

BODY=$(
  {
    echo "loseit-scraper STALE at $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "host: $(hostname)"
    echo
    echo "$REASON"
    echo
    echo "Likely causes: Mac was asleep/off at 06:00, launchd job unloaded,"
    echo "scraper failing every run, or session expired (re-run: python src/scraper.py login)."
    if [[ -f "${DATA_DIR}/logs/scraper.log" ]]; then
      echo
      echo "---- last 40 lines of ${DATA_DIR}/logs/scraper.log ----"
      tail -n 40 "${DATA_DIR}/logs/scraper.log"
    fi
  }
)

if [[ -n "$ALERT_EMAIL" ]]; then
  echo "$BODY" | mail -s "loseit-scraper STALE (no recent success)" "$ALERT_EMAIL" || true
else
  echo "WARNING: scrape appears stale; set LOSEIT_ALERT_EMAIL to enable email alerts" >&2
  echo "$BODY" >&2
fi

echo "$TODAY" >"$STALE_ALERT_STAMP"
exit 0
