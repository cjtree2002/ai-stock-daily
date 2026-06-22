#!/bin/bash
# Daily AI stock news report runner
# Called by launchd at 8:00 AM every weekday

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/run.log"
mkdir -p "$SCRIPT_DIR/logs"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG_FILE"

# Load NEWS_API_KEY from .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

if [ -z "$NEWS_API_KEY" ]; then
  echo "ERROR: NEWS_API_KEY not set" >> "$LOG_FILE"
  exit 1
fi

# Use system Python or venv
PYTHON="python3"
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
  PYTHON="$SCRIPT_DIR/.venv/bin/python"
fi

cd "$SCRIPT_DIR"
$PYTHON fetch_news.py >> "$LOG_FILE" 2>&1

# Open the latest report in default browser
LATEST="$SCRIPT_DIR/reports/latest.html"
if [ -f "$LATEST" ]; then
  open "$LATEST"
fi

echo "Done" >> "$LOG_FILE"
