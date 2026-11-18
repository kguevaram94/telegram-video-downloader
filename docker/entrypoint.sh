#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR" /data/videos

if [ ! -f /app/config.env ]; then
  echo "Falta /app/config.env. Monta tu config.env en el contenedor."
  exit 1
fi

DOWNLOADER_PID=""

start_downloader() {
  echo "Iniciando downloader..."
  python -u /app/downloader.py >>"${LOG_PATH:-$DATA_DIR/downloader.log}" 2>&1 &
  DOWNLOADER_PID=$!
}

stop_downloader() {
  if [ -n "${DOWNLOADER_PID}" ] && kill -0 "$DOWNLOADER_PID" 2>/dev/null; then
    kill "$DOWNLOADER_PID" 2>/dev/null || true
    wait "$DOWNLOADER_PID" 2>/dev/null || true
  fi
  pkill -f "/app/downloader.py" 2>/dev/null || true
}

shutdown() {
  echo "Deteniendo servicios..."
  stop_downloader
  if [ -n "${DASHBOARD_PID:-}" ] && kill -0 "$DASHBOARD_PID" 2>/dev/null; then
    kill "$DASHBOARD_PID" 2>/dev/null || true
    wait "$DASHBOARD_PID" 2>/dev/null || true
  fi
  exit 0
}

trap shutdown INT TERM

MODE="${1:-all}"

case "$MODE" in
  downloader)
    exec python -u /app/downloader.py
    ;;
  dashboard)
    exec python -u /app/dashboard.py
    ;;
  all)
    start_downloader
    python -u /app/dashboard.py &
    DASHBOARD_PID=$!
    wait "$DASHBOARD_PID"
    ;;
  *)
    echo "Uso: entrypoint.sh [all|downloader|dashboard]"
    exit 1
    ;;
esac
