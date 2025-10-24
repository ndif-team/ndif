#!/bin/bash
PORT="${QUEUE_PORT:-6001}"

# This service currently does not support multiple workers.
# In order to support multiple workers, we would need to implement a shared queue (e.g. Redis)
gunicorn src.app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker --timeout 120

