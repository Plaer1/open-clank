#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PORT="${APP_PORT:-7000}"
URL="http://127.0.0.1:${PORT}"

if fuser -s "${PORT}/tcp"; then
  echo "Stopping Open-Clank on port ${PORT}..."
  fuser -k -TERM "${PORT}/tcp" || true
  for _ in {1..30}; do
    fuser -s "${PORT}/tcp" || break
    sleep 0.2
  done
  if fuser -s "${PORT}/tcp"; then
    fuser -k "${PORT}/tcp" || true
  fi
fi

if fuser -s "${PORT}/tcp"; then
  echo "Port ${PORT} is still in use."
  exit 1
fi

DEBUG=false APP_PORT="${PORT}" .venv/bin/python app.py &
app_pid=$!
trap 'kill "${app_pid}" 2>/dev/null || true' EXIT INT TERM

for _ in {1..40}; do
  if curl -fsS --max-time 1 "${URL}/login" >/dev/null 2>&1; then
    xdg-open "${URL}" >/dev/null 2>&1 &
    wait "${app_pid}"
    exit $?
  fi
  if ! kill -0 "${app_pid}" 2>/dev/null; then
    wait "${app_pid}"
  fi
  sleep 0.25
done

echo "Open-Clank did not become ready at ${URL}."
exit 1
