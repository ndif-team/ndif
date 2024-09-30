#!/bin/bash

# This script is used to start the API service inside the Docker container.
# It is executed as the CMD instruction in the Dockerfile.
#
# The script does the following:
# 1. Starts the Gunicorn server to run the FastAPI application
# 2. Binds the server to 0.0.0.0:80 (internal to the container)
# 3. Uses the number of workers specified by the $WORKERS environment variable
# 4. Utilizes the Uvicorn worker class for ASGI support
# 5. Sets a timeout of 120 seconds for worker processes
#
# Note: This script assumes that the Python environment has been activated
# and all necessary dependencies are installed in the container.

gunicorn src.app:app --bind 0.0.0.0:80 --workers $WORKERS --worker-class uvicorn.workers.UvicornWorker --timeout 120

