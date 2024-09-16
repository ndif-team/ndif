#!/bin/bash
pip install python-logging-loki

gunicorn src.app:app --bind 0.0.0.0:80 --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120

