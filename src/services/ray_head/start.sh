#!/bin/bash

resources=`python -m src.ray.resources --head`

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
 
serve deploy src/ray/config/ray_config.yml

tail -f /dev/null