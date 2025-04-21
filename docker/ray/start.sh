#!/bin/bash

# Install the volume-mounted packages (to avoid rebuilding the image for every source code change)
pip install -e /ndif-core
pip install -e /ndif-ray

# Obtain the resources required for the Ray cluster
resources=`python -m ndif.ray.resources --head`

# Start Ray with environment variables from env_file
ray start --head \
    --resources="$resources" \
    --port=$RAY_HEAD_INTERNAL_PORT \
    --object-manager-port=$OBJECT_MANAGER_PORT \
    --include-dashboard=true \
    --dashboard-host=$RAY_DASHBOARD_HOST \
    --dashboard-port=$RAY_DASHBOARD_INTERNAL_PORT \
    --dashboard-grpc-port=$RAY_DASHBOARD_GRPC_PORT \
    --metrics-export-port=$RAY_SERVE_INTERNAL_PORT
 
# Deploy the Ray cluster
serve deploy ndif-ray/src/ndif/ray/config/ray_config.yml

tail -f /dev/null