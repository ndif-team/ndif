#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="monitor"
CONDA_BASE="$(conda info --base)"
PYTHON="${CONDA_BASE}/envs/${ENV_NAME}/bin/python3"

INSTALL_DIR="${INSTALL_DIR:-$HOME/ndif_monitor}"
MONITOR_CRON="${MONITOR_CRON:-*/10 * * * *}"

# --- Ensure conda env exists ---
if ! conda env list | grep -q "^${ENV_NAME} "; then
    echo "Creating conda env '${ENV_NAME}' with Python 3.12..."
    conda create -n "$ENV_NAME" python=3.12 -y
fi

echo "Installing/upgrading dependencies..."
conda run -n "$ENV_NAME" pip install --upgrade nnsight requests flask --quiet

# --- Set up install directory structure ---
mkdir -p "${INSTALL_DIR}/jobs" "${INSTALL_DIR}/dashboard" "${INSTALL_DIR}/logs"

# --- Copy source into install dir ---
echo "Copying source to ${INSTALL_DIR}..."
cp "${SCRIPT_DIR}/jobs/monitor.py" "${SCRIPT_DIR}/jobs/util.py" "${INSTALL_DIR}/jobs/"
cp "${SCRIPT_DIR}/dashboard/dashboard.py" "${SCRIPT_DIR}/dashboard/dashboard.html" "${INSTALL_DIR}/dashboard/"

# --- Ensure config exists ---
CONFIG="${INSTALL_DIR}/config.json"
if [ ! -f "$CONFIG" ]; then
    cp "${SCRIPT_DIR}/config.example.json" "$CONFIG"
    echo "Created ${CONFIG} from example — edit it with your discord webhook URL."
fi

# --- Check for NDIF_API_KEY ---
if [ -z "${NDIF_API_KEY:-}" ]; then
    echo "Warning: NDIF_API_KEY is not set. Model checks will be skipped."
    echo "Set it in your environment or pass it before running this script:"
    echo "  NDIF_API_KEY=your_key ./run.sh"
fi

# --- Install/update cron job ---
JOBS_DIR="${INSTALL_DIR}/jobs"
LOGS_DIR="${INSTALL_DIR}/logs"
MARKER="# ndif-monitor"
(crontab -l 2>/dev/null | grep -v "$MARKER") | cat - <<EOF | crontab -
${MONITOR_CRON} PYTHONPATH=${JOBS_DIR} NDIF_API_KEY=${NDIF_API_KEY:-} ${PYTHON} ${JOBS_DIR}/monitor.py --log-dir ${LOGS_DIR} --config ${CONFIG} ${MARKER}
EOF

echo ""
echo "Done. Cron job installed:"
crontab -l | grep "$MARKER"
echo ""
echo "Install dir: ${INSTALL_DIR}"
echo "  jobs/       — monitor scripts"
echo "  dashboard/  — dashboard server"
echo "  logs/       — check logs"
echo "  config.json — discord config"
echo "Conda env:   ${ENV_NAME} (${PYTHON})"
echo "Dashboard:   ${PYTHON} ${INSTALL_DIR}/dashboard/dashboard.py --log-dir ${LOGS_DIR}"
