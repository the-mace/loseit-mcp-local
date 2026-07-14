#!/bin/bash
# Unload and remove the LoseIt scraper launchd agents (current + legacy labels).
set -euo pipefail

LAUNCH_AGENTS="${HOME}/Library/LaunchAgents"
LABELS=(
  "com.loseit-mcp.scraper"
  "com.loseit-mcp.scraper-health"
  "com.rob.loseit-scraper"
  "com.rob.loseit-scraper-health"
)

for label in "${LABELS[@]}"; do
  plist="${LAUNCH_AGENTS}/${label}.plist"
  if [[ -f "$plist" ]]; then
    launchctl unload "$plist" 2>/dev/null || true
    rm -f "$plist"
    echo "removed ${plist}"
  fi
  launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
done

echo "Done."
