#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:${PORT:-8765}}"
OUT_DIR="${OUT_DIR:-/tmp}"
CHROME="${CHROME:-chromium}"

if ! command -v "$CHROME" >/dev/null 2>&1; then
  echo "ERROR: chromium not found; set CHROME=/path/to/browser" >&2
  exit 1
fi

curl -sf --max-time 4 "$BASE_URL/api/notes" >/dev/null
curl -sf --max-time 4 "$BASE_URL/api/export/ai" | grep -q '"schema": "copal.ai-export.v0"'

"$CHROME" \
  --headless=new \
  --disable-gpu \
  --no-sandbox \
  --user-data-dir=/tmp/copal-chromium-notes-smoke \
  --window-size=1280,900 \
  --virtual-time-budget=5000 \
  --screenshot="$OUT_DIR/copal-notes-1280x900.png" \
  "$BASE_URL/#tab=notes" >/dev/null

"$CHROME" \
  --headless=new \
  --disable-gpu \
  --no-sandbox \
  --user-data-dir=/tmp/copal-chromium-wiki-mobile-smoke \
  --window-size=390,844 \
  --virtual-time-budget=5000 \
  --screenshot="$OUT_DIR/copal-wiki-390x844.png" \
  "$BASE_URL/#tab=wiki" >/dev/null

echo "browser_smoke=ok"
echo "notes_screenshot=$OUT_DIR/copal-notes-1280x900.png"
echo "wiki_mobile_screenshot=$OUT_DIR/copal-wiki-390x844.png"
