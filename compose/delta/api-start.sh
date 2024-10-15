#!/bin/bash
pip install --upgrade ray[serve]==2.31.0
gunicorn src.app:app --bind 0.0.0.0:80 --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120

