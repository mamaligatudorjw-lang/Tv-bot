#!/usr/bin/env bash
# Production startup: run Flask bot (port 5000) + Node API server (port 8080) together.
# The api-server proxies /bot-api/* to the Flask bot on localhost:5000.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[start-production] Starting Flask Telegram bot on :5000 ..."
cd "$ROOT_DIR/telegram-webhook-bot"
"$ROOT_DIR/.pythonlibs/bin/gunicorn" \
  --bind 0.0.0.0:5000 \
  --workers 1 \
  --timeout 120 \
  --keep-alive 5 \
  --log-level info \
  app:app &
FLASK_PID=$!
echo "[start-production] Flask bot PID=$FLASK_PID"

# Wait until Flask is ready before starting Node (so early proxy calls don't fail)
echo "[start-production] Waiting for Flask bot to be ready ..."
for i in $(seq 1 20); do
  if curl -sf http://127.0.0.1:5000/bot-api/status > /dev/null 2>&1; then
    echo "[start-production] Flask bot is ready."
    break
  fi
  sleep 1
done

echo "[start-production] Starting Node API server on :${PORT:-8080} ..."
cd "$ROOT_DIR"
exec node --enable-source-maps artifacts/api-server/dist/index.mjs
