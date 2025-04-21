#!/bin/bash

# Install the volume-mounted packages (to avoid rebuilding the image for every source code change)
pip install -e /ndif-core
pip install -e /ndif-api


gunicorn ndif.api.app:app --bind 0.0.0.0:80 --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120

