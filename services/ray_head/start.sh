#!/bin/bash

# This script starts the Ray head node for the NDIF (National Deep Inference Fabric) project from inside the ray_head container.
#
# It performs the following steps:
# 1. Determines the available resources for the head node
# 3. Starts the Ray head node with specified configuration
# 4. Deploys the Ray Serve application
# 5. Keeps the container running
#
# Environment variables (from .env file):
#   RAY_HEAD_INTERNAL_PORT: Port for Ray head node
#   OBJECT_MANAGER_PORT: Port for Ray's object manager
#   RAY_DASHBOARD_HOST: Host for Ray dashboard
#   RAY_DASHBOARD_INTERNAL_PORT: Internal port for Ray dashboard
#   RAY_DASHBOARD_GRPC_PORT: gRPC port for Ray dashboard
#   RAY_SERVE_INTERNAL_PORT: Internal port for Ray Serve

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