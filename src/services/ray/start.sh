#!/bin/bash

# Compute resources for Ray
resources=`python -m src.ray.resources --head`

# Ensure temp directory exists and is writable
mkdir -p "$NDIF_RAY_TEMP_DIR" 2>/dev/null
if [ ! -w "$NDIF_RAY_TEMP_DIR" ]; then
    echo "ERROR: Cannot write to Ray temp directory: $NDIF_RAY_TEMP_DIR" >&2
    echo "Set NDIF_RAY_TEMP_DIR to a writable location." >&2
    exit 1
fi

echo "Starting Ray head node..."
echo "  Head port: ${NDIF_RAY_HEAD_PORT}"
echo "  Dashboard port: ${NDIF_RAY_DASHBOARD_PORT}"

# Start Ray with environment variables
ray start --head \
    --resources="$resources" \
    --port=${NDIF_RAY_HEAD_PORT} \
    --object-manager-port=${NDIF_RAY_OBJECT_MANAGER_PORT} \
    --include-dashboard=true \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=${NDIF_RAY_DASHBOARD_PORT} \
    --dashboard-agent-grpc-port=${NDIF_RAY_DASHBOARD_GRPC_PORT} \
    --metrics-export-port=${NDIF_RAY_SERVE_PORT} \
    --temp-dir="${NDIF_RAY_TEMP_DIR}"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start Ray head node" >&2
    exit 1
fi

echo "Starting NDIF controller..."
python -m src.ray.start ${NDIF_CONTROLLER_IMPORT_PATH}

tail -f /dev/null
