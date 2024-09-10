#!/bin/bash

resources=`python -m src.ray.resources --head`

ray start --head \
    --resources="$resources" \
    --port=6379 \
    --object-manager-port=8076 \
    --include-dashboard=true \
    --dashboard-host=0.0.0.0 \
    --dashboard-port=8265 \
    --dashboard-grpc-port=8268 \
    --metrics-export-port=8267
    
    
serve deploy src/ray/config/ray_config.yml

tail -f /dev/null