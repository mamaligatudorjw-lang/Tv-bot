#!/usr/bin/env bash
# Production startup: run Flask bot (port 5000) + Node API server (port 8080) together.
# The api-server proxies /bot-api/* to the Flask bot on localhost:5000.
#
# Important: the artifact manager waits for port 8080 on a short readiness
# window. Start Node before waiting for Flask so the routed API port opens
# immediately; /bot-api can return 502 briefly while Flask is booting.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

FLASK_PID=""
NODE_PID=""

cleanup() {
  local status=$?
  trap - EXIT INT TERM

  if [[ -n "$NODE_PID" ]] && kill -0 "$NODE_PID" 2>/dev/null; then
    kill "$NODE_PID" 2>/dev/null || true
  fi
  if [[ -n "$FLASK_PID" ]] && kill -0 "$FLASK_PID" 2>/dev/null; then
    kill "$FLASK_PID" 2>/dev/null || true
  fi

  wait "$NODE_PID" 2>/dev/null || true
  wait "$FLASK_PID" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT INT TERM

echo "[start-production] Starting Flask Telegram bot on :5000 ..."
# Explicitly enable command polling in production (was previously blocked by
# BOT_POLLING_DISABLED inherited from the artifact env).
unset BOT_POLLING_DISABLED
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

# Start the routed API immediately. The artifact manager needs this port to
# open before it starts health polling; the proxy itself tolerates Flask
# becoming ready a little later.
echo "[start-production] Starting Node API server on :${PORT:-8080} ..."
cd "$ROOT_DIR"
node --enable-source-maps artifacts/api-server/dist/index.mjs &
NODE_PID=$!
echo "[start-production] Node API PID=$NODE_PID"

# Check Flask liveness independently without using a protected operational
# endpoint or the state-locked /health snapshot. This is bounded telemetry
# only and must never delay opening the API artifact port.
echo "[start-production] Waiting for Flask bot to be ready ..."
flask_ready=0
# The last production cold-start reached the end of the old probe loop after
# about 50s wall-clock (20 attempts included curl timeouts). Allow 75s total:
# the observed startup plus ~25s of explicit headroom, while Node is already
# serving the routed API port.
FLASK_READINESS_TIMEOUT_SECONDS=75
readiness_deadline=$((SECONDS + FLASK_READINESS_TIMEOUT_SECONDS))
while (( SECONDS < readiness_deadline )); do
  remaining_seconds=$((readiness_deadline - SECONDS))
  probe_timeout_seconds=$((remaining_seconds < 2 ? remaining_seconds : 2))
  if (( probe_timeout_seconds < 1 )); then
    break
  fi

  if curl --fail --silent --show-error \
      --connect-timeout "$probe_timeout_seconds" \
      --max-time "$probe_timeout_seconds" \
      http://127.0.0.1:5000/ping > /dev/null 2>&1; then
    echo "[start-production] Flask bot is ready."
    flask_ready=1
    break
  fi

  if (( SECONDS + 1 < readiness_deadline )); then
    sleep 1
  else
    break
  fi
done

if [[ "$flask_ready" -ne 1 ]]; then
  echo "[start-production] WARNING: Flask bot did not become ready within ${FLASK_READINESS_TIMEOUT_SECONDS}s; keeping API process alive."
fi

# Keep the combined service alive while both children run. If either child
# exits, cleanup stops the other and propagates the failure to deployment.
wait -n "$FLASK_PID" "$NODE_PID"
