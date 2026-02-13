#!/bin/bash
# Simulate Ray backend failure by restarting the Ray container

set -e

CONTAINER_NAME="dev-ray-1"
KILL_MODE=false
DELAY=5

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --kill)
            KILL_MODE=true
            shift
            ;;
        --delay)
            DELAY="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--kill] [--delay SECONDS]"
            exit 1
            ;;
    esac
done

timestamp() {
    date "+%H:%M:%S.%3N"
}

echo "============================================================"
echo "[$(timestamp)] Ray Restart Simulation"
echo "============================================================"
echo "Container: $CONTAINER_NAME"
echo "Kill mode: $KILL_MODE"
echo "Restart delay: ${DELAY}s"
echo ""

# Check if container exists
if ! docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "[$(timestamp)] ERROR: Container $CONTAINER_NAME not found"
    echo "Available containers:"
    docker ps -a --format '{{.Names}}'
    exit 1
fi

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "[$(timestamp)] WARNING: Container $CONTAINER_NAME is not running"
    echo "[$(timestamp)] Starting container..."
    docker start "$CONTAINER_NAME"
    echo "[$(timestamp)] Container started"
    exit 0
fi

# Stop/kill the container
if [ "$KILL_MODE" = true ]; then
    echo "[$(timestamp)] Killing container (SIGKILL)..."
    docker kill "$CONTAINER_NAME"
else
    echo "[$(timestamp)] Stopping container (graceful)..."
    docker stop "$CONTAINER_NAME"
fi

echo "[$(timestamp)] Container stopped"
echo "[$(timestamp)] Waiting ${DELAY}s before restart..."
sleep "$DELAY"

# Start the container
echo "[$(timestamp)] Starting container..."
docker start "$CONTAINER_NAME"

echo "[$(timestamp)] Container started"
echo ""
echo "============================================================"
echo "[$(timestamp)] Ray restart simulation complete"
echo "============================================================"
echo ""
echo "Note: Ray may take 30-60 seconds to fully initialize."
echo "Check Ray dashboard at http://localhost:8265 for status."
