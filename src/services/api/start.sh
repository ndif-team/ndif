#!/bin/bash

# Use NDIF_API_PORT if defined, otherwise default to 8001
PORT="${NDIF_API_PORT:-8001}"
WORKERS="${NDIF_API_WORKERS:-1}"

gunicorn --config src/gunicorn.conf.py src.app:app --bind 0.0.0.0:$PORT --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120
