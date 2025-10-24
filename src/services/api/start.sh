#!/bin/bash

# Use API_INTERNAL_PORT if defined, otherwise default to 80
PORT="${API_INTERNAL_PORT:-80}"

gunicorn --config src/gunicorn.conf.py src.app:app --bind 0.0.0.0:$PORT --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120
