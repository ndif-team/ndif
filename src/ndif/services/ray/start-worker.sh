#!/bin/bash

# Compute resources for Ray worker
if [ -z "$resource_name" ]; then
    resources=`python -m ndif.services.ray.resources`
else
    resources=`python -m ndif.services.ray.resources --name $resource_name`
fi

# Use NDIF_RAY_TEMP_DIR if set, otherwise fall back to /tmp/ray
RAY_TEMP_DIR="${NDIF_RAY_TEMP_DIR:-/tmp/ray}"
mkdir -p "$RAY_TEMP_DIR"

# Convert ray:// address to IP:PORT format for ray start
# NDIF_RAY_ADDRESS may be "ray://IP:PORT" or "IP:PORT"
# ray start --address needs "IP:HEAD_PORT"
RAY_ADDR_CLEAN=$(echo "${NDIF_RAY_ADDRESS}" | sed 's|ray://||')
RAY_HEAD_IP=$(echo "${RAY_ADDR_CLEAN}" | cut -d':' -f1)
RAY_GCS_ADDRESS="${RAY_HEAD_IP}:${NDIF_RAY_HEAD_PORT}"

echo "Starting Ray worker node..."
echo "  Connecting to: ${RAY_GCS_ADDRESS}"
echo "  Temp dir: $RAY_TEMP_DIR"

ray start --resources "$resources" \
    --address "${RAY_GCS_ADDRESS}" \
    --temp-dir="$RAY_TEMP_DIR"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start Ray worker node" >&2
    exit 1
fi

echo "Ray worker node started successfully."

tail -f /dev/null
