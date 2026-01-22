#!/bin/bash

# Compute resources for Ray
resources=`python -m src.ray.resources --head`

# Use NDIF_RAY_TEMP_DIR if set, otherwise fall back to /tmp/ray
RAY_TEMP_DIR="${NDIF_RAY_TEMP_DIR:-/tmp/ray}"

# Ensure temp directory exists and is writable
mkdir -p "$RAY_TEMP_DIR" 2>/dev/null
if [ ! -w "$RAY_TEMP_DIR" ]; then
    echo "ERROR: Cannot write to Ray temp directory: $RAY_TEMP_DIR" >&2
    echo "Set NDIF_RAY_TEMP_DIR to a writable location." >&2
    exit 1
fi

echo "Starting Ray head node..."
echo "  Temp dir: $RAY_TEMP_DIR"
echo "  Head port: ${NDIF_RAY_HEAD_PORT:-6385}"
echo "  Dashboard: ${NDIF_RAY_DASHBOARD_HOST:-0.0.0.0}:${NDIF_RAY_DASHBOARD_PORT:-8265}"

# Start Ray with environment variables
ray start --head \
    --resources="$resources" \
    --port=${NDIF_RAY_HEAD_PORT:-6385} \
    --object-manager-port=${NDIF_RAY_OBJECT_MANAGER_PORT:-8076} \
    --include-dashboard=true \
    --dashboard-host=${NDIF_RAY_DASHBOARD_HOST:-0.0.0.0} \
    --dashboard-port=${NDIF_RAY_DASHBOARD_PORT:-8265} \
    --dashboard-agent-grpc-port=${NDIF_RAY_DASHBOARD_GRPC_PORT:-8268} \
    --metrics-export-port=${NDIF_RAY_SERVE_PORT:-8267} \
    --temp-dir="$RAY_TEMP_DIR"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start Ray head node" >&2
    exit 1
fi

echo "Starting NDIF controller..."
python -m src.ray.start ${NDIF_CONTROLLER_IMPORT_PATH:-src.ray.deployments.controller.controller}

tail -f /dev/null
