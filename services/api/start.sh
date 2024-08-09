#!/bin/bash

# TODO: Figure out how to get this to install from environment.yml (currently fails to)
pip install prometheus-fastapi-instrumentator

gunicorn src.app:app --bind 0.0.0.0:80 --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120
