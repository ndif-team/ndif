#!/bin/bash

# Compute resources for Ray worker
if [ -z "$resource_name" ]; then
    resources=`python -m src.ray.resources`
else
    resources=`python -m src.ray.resources --name $resource_name`
fi

# Use NDIF_RAY_TEMP_DIR if set, otherwise fall back to /tmp/ray
RAY_TEMP_DIR="${NDIF_RAY_TEMP_DIR:-/tmp/ray}"
mkdir -p "$RAY_TEMP_DIR"

echo "Starting Ray worker node..."
echo "  Connecting to: ${NDIF_RAY_ADDRESS}"
echo "  Temp dir: $RAY_TEMP_DIR"

ray start --resources "$resources" \
    --address ${NDIF_RAY_ADDRESS} \
    --temp-dir="$RAY_TEMP_DIR"

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to start Ray worker node" >&2
    exit 1
fi

echo "Ray worker node started successfully."

tail -f /dev/null
