#!/bin/bash
source /.env

# Start Redis server
echo "Starting Redis server on port ${BROKER_INTERNAL_PORT}..."
redis-server --port ${BROKER_INTERNAL_PORT} --dir /data/redis --daemonize yes
sleep 2

# Start MinIO server
echo "Starting MinIO server on port ${MINIO_INTERNAL_PORT}..."
export MINIO_ROOT_USER=${MINIO_ROOT_USER:-minioadmin}
export MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD:-minioadmin}
minio server /data/minio --address :${MINIO_INTERNAL_PORT} --console-address :9001 &
MINIO_PID=$!
echo "MinIO started with PID $MINIO_PID"
sleep 3

# Start API service in background
echo "Starting API service..."
bash /start_api.sh &
API_PID=$!
echo "API service started with PID $API_PID"
sleep 2

# Start Ray service in foreground
echo "Starting Ray service..."
exec bash /start_ray.sh