#!/bin/bash
# Daily AI stock report opener
# Called by launchd at 9:30 AM every weekday.
# The report itself is generated and published by GitHub Actions
# (cloud, ~09:05 Beijing time); this script just opens the live site.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/run.log"
mkdir -p "$SCRIPT_DIR/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') open live report ===" >> "$LOG_FILE"

# Skip weekends (Sat=6, Sun=7): no new report on non-trading mornings
DOW=$(date +%u)
if [ "$DOW" -ge 6 ]; then
  echo "Weekend, skip." >> "$LOG_FILE"
  exit 0
fi

open "https://cjtree2002.github.io/ai-stock-daily/"
echo "Done" >> "$LOG_FILE"
