#!/bin/bash
#
# Start the NDIF API: gunicorn (uvicorn workers) serving the FastAPI app, with
# the queue dispatcher launched once in the master (see gunicorn_conf.py).
#
# Env: NDIF_API_PORT (8001), NDIF_API_WORKERS (1), NDIF_API_TIMEOUT (120).
set -euo pipefail

# Telemetry: label everything this service emits to Loki as `service=api`.
export NDIF_SERVICE="${NDIF_SERVICE:-api}"

# exec so gunicorn replaces the shell (PID 1) and gets signals directly.
exec gunicorn \
    --config python:ndif.services.api.gunicorn_conf \
    ndif.services.api.app:app
