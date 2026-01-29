#!/bin/bash
# Quick test: send request, restart ray, send another request
# Good for rapid iteration on the fix

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

timestamp() {
    date "+%H:%M:%S.%3N"
}

echo "============================================================"
echo "      Quick Reconnection Test"
echo "============================================================"
echo ""

# Step 1: Send a request before restart
echo "[$(timestamp)] Step 1: Sending request BEFORE restart..."
cd "$SCRIPT_DIR"
python3 send_requests.py --count 1
echo ""

# Step 2: Restart Ray (quick - 2 second delay)
echo "[$(timestamp)] Step 2: Restarting Ray container..."
./simulate_ray_restart.sh --delay 2
echo ""

# Step 3: Wait for Ray to partially come up
echo "[$(timestamp)] Step 3: Waiting 10s for Ray to start initializing..."
sleep 10
echo ""

# Step 4: Try sending requests immediately (should fail or succeed with retry)
echo "[$(timestamp)] Step 4: Sending requests DURING Ray startup..."
python3 send_requests.py --count 2
echo ""

# Step 5: Wait for Ray to fully initialize
echo "[$(timestamp)] Step 5: Waiting 30s for Ray to fully initialize..."
sleep 30
echo ""

# Step 6: Send requests after full restart
echo "[$(timestamp)] Step 6: Sending requests AFTER full restart..."
python3 send_requests.py --count 3
echo ""

# Show API logs
echo "============================================================"
echo "[$(timestamp)] API logs from last 2 minutes:"
echo "============================================================"
docker logs --since 2m dev-api-1 2>&1 | grep -E "(ERROR|Ray|connect|actor)" | tail -30 || true
echo ""
echo "============================================================"
echo "Test complete. Check above for errors and reconnection behavior."
echo "============================================================"
