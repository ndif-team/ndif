#!/usr/bin/env bash
# Canonical run script for the dashboard.
#
# Used by:
#   - the container's ``ndif start dashboard`` (CMD in Dockerfile.dashboard)
#   - standalone dev: ``bash src/ndif/services/dashboard/start.sh``
#
# Behavior:
#   - Always execs uvicorn in the foreground on $NDIF_DASHBOARD_PORT (default 8081).
#   - Additionally starts a cron daemon for the monitor + reconcile jobs IF
#     ``cron`` is on PATH and ``/etc/cron.d`` is writable (this is the case
#     inside the container, where we install cron via apt and run as root).
#     Outside the container the check fails harmlessly and uvicorn runs alone.
#
# Required env (set by docker-compose / your shell):
#   NDIF_DASHBOARD_DATA_DIR              where logs / schedule.json / state live
#   NDIF_DASHBOARD_USERNAME              admin username (default: admin)
#   NDIF_DASHBOARD_PASSWORD_HASH         bcrypt hash of admin password
#   NDIF_DASHBOARD_SESSION_SECRET        random secret for cookie signing
#   NDIF_API_URL                         used by /api/status proxy (default: http://localhost:5001)
#   NDIF_RAY_ADDRESS                     for the reconcile cron's deploy/evict calls
#   NDIF_BROKER_URL                      for the reconcile cron's deploy/evict calls
# Optional:
#   NDIF_DASHBOARD_PORT                  default 8081
#   NDIF_DASHBOARD_FRONTEND_DIST         default <package>/frontend/dist
#   NDIF_DASHBOARD_MONITOR_URL           what the monitor cron probes; default http://localhost:5001
#   NDIF_API_KEY                         needed by the monitor cron's model traces
#   NDIF_DASHBOARD_MONITOR_CRON          default "*/10 * * * *"
#   NDIF_DASHBOARD_RECONCILE_CRON        default "*/2 * * * *"
set -euo pipefail

PORT="${NDIF_DASHBOARD_PORT:-8081}"
DATA_DIR="${NDIF_DASHBOARD_DATA_DIR:-${HOME}/ndif_dashboard}"
mkdir -p "$DATA_DIR/logs"

# Stage a default config.json on first run so the monitor cron's discord
# template lookups don't blow up.
CONFIG="$DATA_DIR/config.json"
if [ ! -f "$CONFIG" ]; then
    EXAMPLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.example.json"
    if [ -f "$EXAMPLE" ]; then
        cp "$EXAMPLE" "$CONFIG"
    fi
fi

# ---- Cron daemon (container-only path) -------------------------------------
if command -v cron >/dev/null 2>&1 && [ -w /etc/cron.d ]; then
    CRON_FILE="/etc/cron.d/ndif-dashboard"
    PYTHON="$(command -v python3 || command -v python)"
    LOG_DIR="$DATA_DIR/logs"

    cat > "$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
NDIF_DASHBOARD_DATA_DIR=$DATA_DIR
NDIF_API_URL=${NDIF_API_URL:-http://api:5001}
NDIF_API_KEY=${NDIF_API_KEY:-}
NDIF_RAY_ADDRESS=${NDIF_RAY_ADDRESS:-}
NDIF_BROKER_URL=${NDIF_BROKER_URL:-}

${NDIF_DASHBOARD_MONITOR_CRON:-*/10 * * * *} root $PYTHON -m ndif.services.dashboard.jobs.monitor --url ${NDIF_DASHBOARD_MONITOR_URL:-http://localhost:5001} --log-dir $LOG_DIR --config $CONFIG >> $LOG_DIR/monitor.cron.log 2>&1
${NDIF_DASHBOARD_RECONCILE_CRON:-*/2 * * * *} root $PYTHON -m ndif.services.dashboard.jobs.reconcile >> $LOG_DIR/reconcile.cron.log 2>&1
EOF
    chmod 0644 "$CRON_FILE"

    echo "[dashboard] starting cron daemon"
    cron
else
    echo "[dashboard] cron not available — schedules won't run from this start.sh."
    echo "             (intended for non-Docker dev; the docker container runs cron)"
fi

# ---- uvicorn (foreground; PID 1 in the container) --------------------------
echo "[dashboard] starting uvicorn on :$PORT"
echo "[dashboard]   data dir:    $DATA_DIR"
echo "[dashboard]   ndif API:    ${NDIF_API_URL:-http://localhost:5001}"
echo "[dashboard]   monitor URL: ${NDIF_DASHBOARD_MONITOR_URL:-http://localhost:5001}"

exec python -m uvicorn ndif.services.dashboard.backend.app:app \
    --host 0.0.0.0 --port "$PORT"
