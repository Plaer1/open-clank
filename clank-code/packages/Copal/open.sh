#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Copal — one-click launcher.
# Builds the static site on first run, starts the tiny Python server (app.py),
# waits until it answers, opens the browser. Ctrl+C stops the server cleanly.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
cd "$SCRIPT_DIR"

export PORT="${PORT:-8765}"
URL="http://localhost:${PORT}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: 'python3' not found on PATH." >&2
  exit 1
fi

# Build the static site when the build is missing OR stale (any source file
# newer than out/index.html). Without this, editing src/ and restarting just
# re-serves the old build.
build_needed=0
if [ ! -f "out/index.html" ]; then
  build_needed=1
elif [ -n "$(find src public next.config.ts tsconfig.json package.json bun.lock \
              -newer out/index.html -print -quit 2>/dev/null)" ]; then
  build_needed=1
fi

if [ "$build_needed" -eq 1 ]; then
  echo "▶ Building the site (needs 'bun') ..."
  if ! command -v bun >/dev/null 2>&1; then
    echo "ERROR: 'bun' not found — install it to build: https://bun.sh" >&2
    exit 1
  fi
  bun install
  bun run build
fi

if [ "${COPAL_FORCE_PYTHON:-0}" != "1" ]; then
  # Pick the NEWEST binary, not release-first: release builds embed the UI at
  # compile time, so a stale release silently serves an old app while debug
  # (which reads out/ from disk) is fresh. Newest-mtime wins.
  NATIVE_BIN=""
  NATIVE_BIN_MTIME=0
  for candidate in \
    "/tmp/copal-servo-target/release/copal-servo-shell" \
    "/tmp/copal-servo-target/debug/copal-servo-shell" \
    "$SCRIPT_DIR/servo-shell/target/release/copal-servo-shell" \
    "$SCRIPT_DIR/servo-shell/target/debug/copal-servo-shell"
  do
    if [ -x "$candidate" ]; then
      mtime=$(stat -c %Y "$candidate" 2>/dev/null || echo 0)
      if [ "$mtime" -gt "$NATIVE_BIN_MTIME" ]; then
        NATIVE_BIN="$candidate"
        NATIVE_BIN_MTIME="$mtime"
      fi
    fi
  done
  if [ -n "$NATIVE_BIN" ] && [ -f "out/index.html" ]; then
    out_mtime=$(stat -c %Y out/index.html 2>/dev/null || echo 0)
    case "$NATIVE_BIN" in
      */release/*)
        if [ "$out_mtime" -gt "$NATIVE_BIN_MTIME" ]; then
          echo "⚠ release binary is older than the current UI build (embedded assets are stale)."
          echo "  Rebuild it with: bun run servo:native-release"
        fi
        ;;
    esac
  fi

  if [ -n "$NATIVE_BIN" ]; then
    echo "▶ Starting Copal native binary (DB-backed; COPAL_SOURCE=files to revert)."
    echo "  mode=native"
    echo "  binary=$NATIVE_BIN"
    NATIVE_LOG="$(mktemp)"
    COPAL_NATIVE_API_ONLY=1 "$NATIVE_BIN" >"$NATIVE_LOG" 2>&1 &
    NATIVE_PID=$!
    native_cleanup() {
      echo
      echo "■ Stopping native server (PID $NATIVE_PID) ..."
      kill "$NATIVE_PID" 2>/dev/null || true
      wait "$NATIVE_PID" 2>/dev/null || true
    }
    trap native_cleanup EXIT INT TERM
    NATIVE_URL=""
    for _ in $(seq 1 80); do
      NATIVE_URL="$(sed -n 's/^native_api_url=//p' "$NATIVE_LOG" | tail -1)"
      if [ -n "$NATIVE_URL" ] && curl -sf -o /dev/null --max-time 1 "$NATIVE_URL/" 2>/dev/null; then
        break
      fi
      if ! kill -0 "$NATIVE_PID" 2>/dev/null; then
        echo "ERROR: native binary exited during startup:" >&2
        cat "$NATIVE_LOG" >&2
        exit 1
      fi
      sleep 0.25
    done
    if [ -z "$NATIVE_URL" ]; then
      echo "ERROR: native server did not become ready." >&2
      cat "$NATIVE_LOG" >&2
      exit 1
    fi
    sed 's/^/  /' "$NATIVE_LOG"
    echo "✓ Ready! Opening $NATIVE_URL in your browser."
    xdg-open "$NATIVE_URL" >/dev/null 2>&1 || echo "(Could not auto-open — visit $NATIVE_URL manually.)"
    echo "  Stop: Ctrl+C"
    wait "$NATIVE_PID"
    exit 0
  fi

  echo "Native binary not found; using Python/browser fallback."
else
  echo "COPAL_FORCE_PYTHON=1; using Python/browser fallback."
fi

is_current_server() {
  local url="$1"
  curl -sf -o /dev/null --max-time 2 "$url/" 2>/dev/null &&
    curl -sf --max-time 2 "$url/api/export/ai" 2>/dev/null |
      grep -q '"schema": "copal.ai-export.v0"' &&
    curl -s --max-time 2 "$url/api/vault-asset?path=missing.png" 2>/dev/null |
      grep -q '"asset not found"'
}

port_responds() {
  local port="$1"
  curl -sf -o /dev/null --max-time 1 "http://localhost:${port}/" 2>/dev/null
}

# Already running? Open only if it is the current Copal server. If the default
# port has a stale older app.py loaded, prefer another compatible live server
# or start a fresh one on the next free port.
if is_current_server "$URL"; then
  echo "Server already up at $URL — opening browser."
  xdg-open "$URL" >/dev/null 2>&1 || true
  exit 0
fi

for p in $(seq 8766 8785); do
  candidate="http://localhost:${p}"
  if is_current_server "$candidate"; then
    echo "Current Copal server already up at $candidate — opening browser."
    xdg-open "$candidate" >/dev/null 2>&1 || true
    exit 0
  fi
done

if port_responds "$PORT"; then
  echo "Port $PORT is busy with a stale or incompatible server; choosing next free port."
  for p in $(seq 8766 8785); do
    if ! port_responds "$p"; then
      export PORT="$p"
      URL="http://localhost:${PORT}"
      break
    fi
  done
fi

echo "▶ Starting Copal on $URL ..."
python3 app.py &
SERVER_PID=$!

cleanup() {
  echo
  echo "■ Stopping server (PID $SERVER_PID) ..."
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  echo "Stopped."
}
trap cleanup EXIT INT TERM

echo "⏳ Waiting for server ..."
READY=0
for _ in $(seq 1 60); do
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: server exited unexpectedly." >&2
    exit 1
  fi
  if curl -sf -o /dev/null --max-time 2 "$URL/" 2>/dev/null; then
    READY=1
    break
  fi
  sleep 0.5
done

if [ "$READY" -ne 1 ]; then
  echo "ERROR: server did not become ready." >&2
  exit 1
fi

echo "✓ Ready! Opening $URL in your browser."
xdg-open "$URL" >/dev/null 2>&1 || echo "(Could not auto-open — visit $URL manually.)"

echo
echo "────────────────────────────────────────────────────────"
echo "  Copal is live at $URL"
echo "  Data file: $SCRIPT_DIR/move-data.json  (edit by hand any time)"
echo "  Stop:      Ctrl+C"
echo "────────────────────────────────────────────────────────"

# Keep the script alive so the trap only fires on Ctrl+C / kill.
wait "$SERVER_PID"
