#!/usr/bin/env bash
# Set up (or refresh) the NDIF dashboard on the head node.
#
# - Creates/updates a `dashboard` conda env with Python 3.12
# - Installs the ndif package + dashboard requirements into it
# - Builds the Vue frontend (`npm install && npm run build`) if `npm` is found
# - Stages a config.json from the example if one doesn't already exist
# - Installs two cron jobs:
#     */10 * * * *   — monitor (uptime + model traces)
#     */10 * * * *   — reconcile (push schedule.json → controller); offset by 1 min
# - Prints the uvicorn command to start the FastAPI backend
#
# It does NOT run the FastAPI backend itself — start that under your process
# manager of choice (systemd / pm2 / nohup) using the printed command.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
ENV_NAME="dashboard"
CONDA_BASE="$(conda info --base)"
PYTHON="${CONDA_BASE}/envs/${ENV_NAME}/bin/python3"

INSTALL_DIR="${DASHBOARD_DATA_DIR:-$HOME/ndif_dashboard}"
MONITOR_CRON="${MONITOR_CRON:-*/10 * * * *}"
RECONCILE_CRON="${RECONCILE_CRON:-*/2 * * * *}"  # snappy enough for "deploy now" UX

# --- Ensure conda env exists ---
if ! conda env list | grep -q "^${ENV_NAME} "; then
    echo "Creating conda env '${ENV_NAME}' with Python 3.12..."
    conda create -n "$ENV_NAME" python=3.12 -y
fi

echo "Installing/upgrading ndif from ${REPO_ROOT}..."
conda run -n "$ENV_NAME" pip install --upgrade "${REPO_ROOT}" --quiet
echo "Installing dashboard backend deps..."
conda run -n "$ENV_NAME" pip install --upgrade -r "${SCRIPT_DIR}/requirements.in" --quiet

# --- Build the frontend if we can ---
if command -v npm >/dev/null 2>&1; then
    pushd "${SCRIPT_DIR}/frontend" >/dev/null
    if [ ! -d node_modules ]; then
        echo "Installing frontend npm dependencies..."
        npm install --silent
    fi
    echo "Building frontend..."
    npm run build --silent
    popd >/dev/null
else
    echo "Warning: npm not found. Skipping frontend build."
    echo "  Install Node 20+ and rerun, or run 'npm install && npm run build' in"
    echo "  ${SCRIPT_DIR}/frontend manually."
fi

# --- Install dir scaffolding ---
mkdir -p "${INSTALL_DIR}/logs"
CONFIG="${INSTALL_DIR}/config.json"
if [ ! -f "$CONFIG" ]; then
    cp "${SCRIPT_DIR}/config.example.json" "$CONFIG"
    echo "Created ${CONFIG} from example — edit it with your discord webhook."
fi

# --- Reminders for required env ---
if [ -z "${NDIF_API_KEY:-}" ]; then
    echo "Warning: NDIF_API_KEY is not set; model traces will be skipped."
fi
if [ -z "${DASHBOARD_PASSWORD_HASH:-}" ]; then
    echo "Warning: DASHBOARD_PASSWORD_HASH is not set."
    echo "  Generate one with:  conda run -n ${ENV_NAME} python -m ndif.services.dashboard.backend.auth hash <password>"
fi
if [ -z "${DASHBOARD_SESSION_SECRET:-}" ]; then
    echo "Warning: DASHBOARD_SESSION_SECRET is not set (defaults to an unsafe placeholder)."
fi

# --- Install/update cron jobs ---
MONITOR_MARKER="# ndif-dashboard-monitor"
RECONCILE_MARKER="# ndif-dashboard-reconcile"
LEGACY_MONITOR_MARKER="# ndif-monitor"  # left over from services/monitor; we replace it

(crontab -l 2>/dev/null \
    | grep -v "$MONITOR_MARKER" \
    | grep -v "$RECONCILE_MARKER" \
    | grep -v "$LEGACY_MONITOR_MARKER"
) | cat - <<EOF | crontab -
${MONITOR_CRON} DASHBOARD_DATA_DIR=${INSTALL_DIR} NDIF_API_KEY=${NDIF_API_KEY:-} ${PYTHON} -m ndif.services.dashboard.jobs.monitor --log-dir ${INSTALL_DIR}/logs --config ${CONFIG} >> ${INSTALL_DIR}/logs/monitor.cron.log 2>&1 ${MONITOR_MARKER}
${RECONCILE_CRON} DASHBOARD_DATA_DIR=${INSTALL_DIR} NDIF_RAY_ADDRESS=${NDIF_RAY_ADDRESS:-} NDIF_BROKER_URL=${NDIF_BROKER_URL:-} ${PYTHON} -m ndif.services.dashboard.jobs.reconcile >> ${INSTALL_DIR}/logs/reconcile.cron.log 2>&1 ${RECONCILE_MARKER}
EOF

echo
echo "Cron entries installed:"
crontab -l | grep -E "$MONITOR_MARKER|$RECONCILE_MARKER"

cat <<INFO

Dashboard data dir: ${INSTALL_DIR}
  logs/        — connected_*.log, models_*.log, cluster_*.log
  schedule.json — calendar-driven deployments (created on first write)
  config.json   — discord webhook etc.

Conda env: ${ENV_NAME} (${PYTHON})

Start the backend (e.g. under systemd / pm2 / nohup), redirecting logs into
the same install dir so everything dashboard-related lives under ${INSTALL_DIR}:

  DASHBOARD_DATA_DIR=${INSTALL_DIR} \\
  DASHBOARD_USERNAME=admin \\
  DASHBOARD_PASSWORD_HASH=<bcrypt hash> \\
  DASHBOARD_SESSION_SECRET=<random 32+ bytes> \\
  DASHBOARD_FRONTEND_DIST=${SCRIPT_DIR}/frontend/dist \\
  ${PYTHON} -m uvicorn ndif.services.dashboard.backend.app:app \\
    --host 0.0.0.0 --port 8081 \\
    >> ${INSTALL_DIR}/logs/uvicorn.log 2>&1

(DASHBOARD_FRONTEND_DIST points at the dist/ that 'npm run build' just produced
in this repo. Without it, FastAPI looks under site-packages/ndif/... where the
non-editable install has no built frontend.)

Frontend dev (live reload):
  cd ${SCRIPT_DIR}/frontend && npm run dev   # serves on :5173, proxies /api to :8081
INFO
