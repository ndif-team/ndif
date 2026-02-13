#!/bin/bash
# Main test script for Ray reconnection behavior
#
# This script:
# 1. Sends some initial requests to verify the system is working
# 2. Starts continuous requests in the background
# 3. Restarts the Ray container
# 4. Monitors if requests resume after restart

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NDIF_DIR="$(dirname "$SCRIPT_DIR")"

# Configuration
INITIAL_REQUESTS=3
CONTINUOUS_INTERVAL=3
CONTINUOUS_WORKERS=2
RAY_RESTART_DELAY=5
WAIT_AFTER_RESTART=60

timestamp() {
    date "+%H:%M:%S.%3N"
}

cleanup() {
    echo ""
    echo "[$(timestamp)] Cleaning up..."
    
    # Kill background processes
    if [ -n "$REQUEST_PID" ]; then
        echo "[$(timestamp)] Stopping request sender (PID: $REQUEST_PID)..."
        kill "$REQUEST_PID" 2>/dev/null || true
        wait "$REQUEST_PID" 2>/dev/null || true
    fi
    
    echo "[$(timestamp)] Cleanup complete"
}

trap cleanup EXIT

echo "============================================================"
echo "      NDIF Ray Reconnection Test"
echo "============================================================"
echo ""
echo "Configuration:"
echo "  Initial requests: $INITIAL_REQUESTS"
echo "  Continuous interval: ${CONTINUOUS_INTERVAL}s"
echo "  Continuous workers: $CONTINUOUS_WORKERS"
echo "  Ray restart delay: ${RAY_RESTART_DELAY}s"
echo "  Wait after restart: ${WAIT_AFTER_RESTART}s"
echo ""
echo "============================================================"
echo ""

# Step 1: Check if containers are running
echo "[$(timestamp)] Step 1: Checking containers..."
if ! docker ps --format '{{.Names}}' | grep -q "dev-ray-1"; then
    echo "[$(timestamp)] ERROR: dev-ray-1 container is not running"
    echo "[$(timestamp)] Run 'make up' in $NDIF_DIR first"
    exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -q "dev-api-1"; then
    echo "[$(timestamp)] ERROR: dev-api-1 container is not running"
    echo "[$(timestamp)] Run 'make up' in $NDIF_DIR first"
    exit 1
fi
echo "[$(timestamp)] Containers are running"
echo ""

# Step 2: Send initial requests to verify system is working
echo "[$(timestamp)] Step 2: Sending $INITIAL_REQUESTS initial requests..."
cd "$SCRIPT_DIR"
python3 send_requests.py --count "$INITIAL_REQUESTS" --workers 1

# Check if initial requests succeeded
if [ $? -ne 0 ]; then
    echo "[$(timestamp)] ERROR: Initial requests failed. Is Ray fully initialized?"
    echo "[$(timestamp)] Wait for Ray to initialize and try again."
    exit 1
fi
echo ""

# Step 3: Start continuous requests in background
echo "[$(timestamp)] Step 3: Starting continuous requests in background..."
python3 send_requests.py \
    --continuous \
    --interval "$CONTINUOUS_INTERVAL" \
    --workers "$CONTINUOUS_WORKERS" \
    > request_output.log 2>&1 &
REQUEST_PID=$!
echo "[$(timestamp)] Request sender started (PID: $REQUEST_PID)"
echo "[$(timestamp)] Output being logged to: $SCRIPT_DIR/request_output.log"
echo ""

# Wait a bit to ensure requests are flowing
echo "[$(timestamp)] Waiting 10s for requests to start flowing..."
sleep 10
echo ""

# Show current request stats
echo "[$(timestamp)] Current request output:"
tail -5 request_output.log || true
echo ""

# Step 4: Restart Ray container
echo "[$(timestamp)] Step 4: Restarting Ray container..."
echo "============================================================"
./simulate_ray_restart.sh --delay "$RAY_RESTART_DELAY"
echo ""

# Step 5: Wait and monitor
echo "[$(timestamp)] Step 5: Monitoring for ${WAIT_AFTER_RESTART}s after restart..."
echo ""

# Monitor in a loop
for i in $(seq 1 $((WAIT_AFTER_RESTART / 10))); do
    echo "[$(timestamp)] --- Status check ($((i * 10))s since restart) ---"
    
    # Check if request process is still running
    if ! kill -0 "$REQUEST_PID" 2>/dev/null; then
        echo "[$(timestamp)] WARNING: Request sender process died!"
        echo "[$(timestamp)] Last output:"
        tail -20 request_output.log || true
        break
    fi
    
    # Show recent request output
    echo "[$(timestamp)] Recent requests:"
    tail -5 request_output.log || true
    echo ""
    
    sleep 10
done

echo ""
echo "============================================================"
echo "[$(timestamp)] Test Complete"
echo "============================================================"
echo ""

# Show final stats
echo "Request log summary:"
echo "  Success: $(grep -c "SUCCESS" request_output.log 2>/dev/null || echo 0)"
echo "  Failed:  $(grep -c "FAILED" request_output.log 2>/dev/null || echo 0)"
echo ""

# Show any errors
echo "Errors encountered:"
grep "FAILED" request_output.log | tail -10 || echo "  (none)"
echo ""

echo "API logs during test (check for reconnection messages):"
docker logs --since 5m dev-api-1 2>&1 | grep -E "(ERROR|Connecting to Ray|Connected to Ray|error)" | tail -20 || true
echo ""

echo "Full request log available at: $SCRIPT_DIR/request_output.log"
echo "To view API logs: docker logs dev-api-1"
