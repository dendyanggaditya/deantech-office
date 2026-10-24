#!/usr/bin/env bash
# Start the DeanTech 3D office: a loopback `hermes serve` backend plus the
# Hermes3D Studio, wired together with a pinned office token.
#
# Usage:  scripts/office.sh          start both, open http://localhost:3000/office
#         scripts/office.sh stop     stop both
#
# The token lives in ~/.hermes/.env as HERMES3D_OFFICE_TOKEN (generated on
# first run). It is passed inline to `hermes serve` only; never pin
# HERMES_DASHBOARD_SESSION_TOKEN in .env, it breaks the Hermes desktop app.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$HOME/.hermes/.env"
BACKEND_PORT="${OFFICE_BACKEND_PORT:-9120}"
STUDIO_PORT="${OFFICE_STUDIO_PORT:-3000}"
STATE_DIR="${HERMES_STATE_DIR:-$HOME/.hermes/deantech-office}"
LOG_DIR="$STATE_DIR/logs"
PID_DIR="$STATE_DIR/run"

stop_pid() {
  local file="$PID_DIR/$1.pid"
  if [[ -f "$file" ]] && kill -0 "$(cat "$file")" 2>/dev/null; then
    kill "$(cat "$file")" && echo "stopped $1"
  fi
  rm -f "$file"
}

if [[ "${1:-}" == "stop" ]]; then
  stop_pid studio
  stop_pid backend
  exit 0
fi

mkdir -p "$LOG_DIR" "$PID_DIR"

token="$(grep -E '^HERMES3D_OFFICE_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)"
if [[ -z "$token" ]]; then
  token="$(openssl rand -hex 32)"
  ( umask 077; printf '\nHERMES3D_OFFICE_TOKEN=%s\n' "$token" >> "$ENV_FILE" )
  echo "generated HERMES3D_OFFICE_TOKEN in $ENV_FILE"
fi

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

if port_busy "$BACKEND_PORT"; then
  echo "port $BACKEND_PORT already in use; assuming hermes serve is running"
else
  HERMES_DASHBOARD_SESSION_TOKEN="$token" nohup hermes serve \
    --host 127.0.0.1 --port "$BACKEND_PORT" --skip-build --isolated \
    >"$LOG_DIR/backend.log" 2>&1 &
  echo $! >"$PID_DIR/backend.pid"
fi

for _ in $(seq 1 30); do
  curl -fs "http://127.0.0.1:$BACKEND_PORT/api/status" >/dev/null && break
  sleep 1
done
curl -fs "http://127.0.0.1:$BACKEND_PORT/api/status" >/dev/null \
  || { echo "hermes serve did not start; see $LOG_DIR/backend.log"; exit 1; }

cd "$ROOT"
[[ -d node_modules ]] || npm install --no-audit --no-fund
[[ -f .next/BUILD_ID ]] || npm run build

if port_busy "$STUDIO_PORT"; then
  echo "port $STUDIO_PORT already in use; stop the other app first"; exit 1
fi

HERMES_STATE_DIR="$STATE_DIR" HOST=127.0.0.1 PORT="$STUDIO_PORT" \
UPSTREAM_ALLOWLIST=localhost,127.0.0.1 \
HERMES3D_GATEWAY_ADAPTER_TYPE=hermes-agent \
HERMES3D_GATEWAY_URL="ws://127.0.0.1:$BACKEND_PORT" \
HERMES3D_GATEWAY_TOKEN="$token" \
  nohup npm run start >"$LOG_DIR/studio.log" 2>&1 &
echo $! >"$PID_DIR/studio.pid"

for _ in $(seq 1 30); do
  curl -fs -o /dev/null "http://127.0.0.1:$STUDIO_PORT/office" && break
  sleep 1
done

echo "office: http://localhost:$STUDIO_PORT/office   (logs: $LOG_DIR)"
command -v open >/dev/null && open "http://localhost:$STUDIO_PORT/office" || true
