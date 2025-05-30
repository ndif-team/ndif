#!/bin/bash

PORT="${QUEUE_PORT:-6001}"
WORKERS="${QUEUE_WORKERS:-1}"

gunicorn src.app:app --bind 0.0.0.0:$PORT --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120

