#!/bin/bash
# Install (or reinstall) the daily scrape + midday health-check launchd agents.
#
# Usage:
#   bash scripts/install-launchd.sh
#   bash scripts/install-launchd.sh --email you@example.com
#   LOSEIT_ALERT_EMAIL=you@example.com bash scripts/install-launchd.sh
#
# Absolute paths are written into ~/Library/LaunchAgents at install time so
# nothing machine-specific needs to live in the repo.
#
# Wrapper scripts are *copied* to ~/.loseit-data/bin/ because macOS TCC
# blocks launchd from executing shell scripts under ~/Documents (exit 126,
# "Operation not permitted"). The Python venv in the project tree is still
# fine — only the shell entrypoints need to live outside Documents.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
DATA_DIR="${HOME}/.loseit-data"
LOG_DIR="${DATA_DIR}/logs"
BIN_DIR="${DATA_DIR}/bin"

LABEL_SCRAPER="com.loseit-mcp.scraper"
LABEL_HEALTH="com.loseit-mcp.scraper-health"
# Legacy labels from earlier personal installs — unload if present.
LEGACY_LABELS=(
  "com.rob.loseit-scraper"
  "com.rob.loseit-scraper-health"
)

ALERT_EMAIL="${LOSEIT_ALERT_EMAIL:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--email ADDRESS]

Install launchd agents for the daily LoseIt scrape (06:00) and stale-run
health check (12:00). Paths are derived from this repo checkout and \$HOME.

Options:
  --email ADDRESS   Set LOSEIT_ALERT_EMAIL in the agents (failure/stale alerts).
                    Also accepted via the LOSEIT_ALERT_EMAIL environment variable.
  -h, --help        Show this help.
EOF
}

xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)
      ALERT_EMAIL="${2:-}"
      if [[ -z "$ALERT_EMAIL" ]]; then
        echo "error: --email requires an address" >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -x "${PROJECT}/.venv/bin/python" ]]; then
  echo "warning: ${PROJECT}/.venv/bin/python not found; create the venv before the first scheduled run." >&2
fi

mkdir -p "$LAUNCH_AGENTS" "$LOG_DIR" "$BIN_DIR"

# Install launchd-facing copies outside ~/Documents (TCC).
cp "${PROJECT}/scripts/run-scraper.sh" "${BIN_DIR}/run-scraper.sh"
cp "${PROJECT}/scripts/health-check.sh" "${BIN_DIR}/health-check.sh"
chmod +x "${BIN_DIR}/run-scraper.sh" "${BIN_DIR}/health-check.sh"
echo "installed wrappers to ${BIN_DIR}/"

project_xml=$(xml_escape "$PROJECT")
env_entries="    <key>LOSEIT_PROJECT</key>
    <string>${project_xml}</string>"
if [[ -n "$ALERT_EMAIL" ]]; then
  email_xml=$(xml_escape "$ALERT_EMAIL")
  env_entries="${env_entries}
    <key>LOSEIT_ALERT_EMAIL</key>
    <string>${email_xml}</string>"
else
  echo "note: no alert email set (pass --email or LOSEIT_ALERT_EMAIL)."
  echo "      Failure/stale conditions will log locally only."
fi

env_block=$(cat <<EOF

  <key>EnvironmentVariables</key>
  <dict>
${env_entries}
  </dict>
EOF
)

write_plist() {
  local label="$1"
  local script_path="$2"
  local hour="$3"
  local out_log="$4"
  local err_log="$5"
  local dest="${LAUNCH_AGENTS}/${label}.plist"

  cat >"$dest" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${script_path}</string>
  </array>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>

  <key>StandardOutPath</key>
  <string>${out_log}</string>
  <key>StandardErrorPath</key>
  <string>${err_log}</string>

  <key>RunAtLoad</key>
  <false/>
${env_block}
</dict>
</plist>
EOF
  echo "wrote ${dest}"
}

unload_label() {
  local label="$1"
  local plist="${LAUNCH_AGENTS}/${label}.plist"
  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
  fi
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
}

for label in "${LEGACY_LABELS[@]}" "$LABEL_SCRAPER" "$LABEL_HEALTH"; do
  unload_label "$label"
done

for label in "${LEGACY_LABELS[@]}"; do
  rm -f "${LAUNCH_AGENTS}/${label}.plist"
done

write_plist \
  "$LABEL_SCRAPER" \
  "${BIN_DIR}/run-scraper.sh" \
  6 \
  "${LOG_DIR}/launchd-scraper.out.log" \
  "${LOG_DIR}/launchd-scraper.err.log"

write_plist \
  "$LABEL_HEALTH" \
  "${BIN_DIR}/health-check.sh" \
  12 \
  "${LOG_DIR}/launchd-health.out.log" \
  "${LOG_DIR}/launchd-health.err.log"

launchctl load "${LAUNCH_AGENTS}/${LABEL_SCRAPER}.plist"
launchctl load "${LAUNCH_AGENTS}/${LABEL_HEALTH}.plist"

echo
echo "Installed:"
echo "  ${LABEL_SCRAPER}   (daily 06:00)"
echo "  ${LABEL_HEALTH}    (daily 12:00)"
echo "  wrappers: ${BIN_DIR}/"
echo "  project:  ${PROJECT}"
if [[ -n "$ALERT_EMAIL" ]]; then
  echo "  alert email: ${ALERT_EMAIL}"
fi
echo
echo "Test immediately:"
echo "  launchctl start ${LABEL_SCRAPER}"
echo "  tail -f ${LOG_DIR}/scraper.log"
echo
echo "Uninstall later:"
echo "  bash scripts/uninstall-launchd.sh"
