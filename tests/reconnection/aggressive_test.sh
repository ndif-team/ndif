#!/bin/bash
# Aggressive test: Short recovery times, concurrent requests, rapid restarts
# Simulates the scenario where head comes up but actors aren't fully ready

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

timestamp() {
    date "+%H:%M:%S.%3N"
}

echo "============================================================"
echo "      Aggressive Recovery Test"
echo "============================================================"
echo ""
echo "This test simulates scenarios where:"
echo "  1. Head node restarts but actors aren't ready"
echo "  2. Requests arrive during partial recovery"
echo "  3. Multiple rapid restart cycles"
echo ""

cd "$SCRIPT_DIR"

# Test 1: Send request immediately after Ray starts (actors not ready)
echo "[$(timestamp)] TEST 1: Request during partial recovery"
echo "============================================================"
docker stop dev-ray-1 > /dev/null 2>&1
sleep 2
docker start dev-ray-1 > /dev/null 2>&1
echo "[$(timestamp)] Ray started, immediately sending request..."

# Send request right away - Ray is starting but actors aren't ready
RESULT=$(timeout 120 python3 send_requests.py --count 1 2>&1)
if echo "$RESULT" | grep -q "SUCCESS"; then
    echo "[$(timestamp)] ✓ Request succeeded (waited for actor deployment)"
    WAIT_TIME=$(echo "$RESULT" | grep -o "in [0-9.]*s" | head -1)
    echo "[$(timestamp)]   Total time: $WAIT_TIME"
else
    echo "[$(timestamp)] ✗ Request failed"
    echo "$RESULT" | tail -5
fi

echo ""

# Test 2: Multiple concurrent requests during recovery
echo "[$(timestamp)] TEST 2: Concurrent requests during recovery"
echo "============================================================"
docker stop dev-ray-1 > /dev/null 2>&1
sleep 2
docker start dev-ray-1 > /dev/null 2>&1
echo "[$(timestamp)] Ray started, sending 3 concurrent requests..."

# Send 3 concurrent requests
python3 send_requests.py --count 3 --workers 3 &
PID=$!
wait $PID
echo ""

# Wait for system to stabilize
echo "[$(timestamp)] Waiting 30s for system to stabilize..."
sleep 30

# Test 3: Very rapid restart cycles (simulates flapping)
echo ""
echo "[$(timestamp)] TEST 3: Rapid restart cycles (flapping simulation)"
echo "============================================================"

for i in 1 2 3; do
    echo "[$(timestamp)] Rapid cycle $i: stop -> 2s -> start -> 5s -> request"
    docker stop dev-ray-1 > /dev/null 2>&1
    sleep 2
    docker start dev-ray-1 > /dev/null 2>&1
    sleep 5
    
    # Try a quick request - will likely fail or take a while
    RESULT=$(timeout 90 python3 send_requests.py --count 1 2>&1)
    if echo "$RESULT" | grep -q "SUCCESS"; then
        WAIT_TIME=$(echo "$RESULT" | grep -o "in [0-9.]*s" | head -1)
        echo "[$(timestamp)]   Cycle $i: SUCCESS $WAIT_TIME"
    else
        echo "[$(timestamp)]   Cycle $i: FAILED"
    fi
done

echo ""

# Let system fully recover for final test
echo "[$(timestamp)] Waiting 60s for full recovery..."
sleep 60

# Test 4: Verify system is healthy after all the abuse
echo ""
echo "[$(timestamp)] TEST 4: Post-abuse health check"
echo "============================================================"
RESULT=$(python3 send_requests.py --count 5 2>&1)
SUCCESS=$(echo "$RESULT" | grep -o "[0-9]* success" | grep -o "[0-9]*")
FAILED=$(echo "$RESULT" | grep -o "[0-9]* failed" | grep -o "[0-9]*")
echo "[$(timestamp)] Health check: $SUCCESS/5 success"

if [ "$SUCCESS" -eq 5 ]; then
    echo "[$(timestamp)] ✓ System recovered fully"
else
    echo "[$(timestamp)] ✗ System may be degraded"
fi

echo ""
echo "============================================================"
echo "Checking API logs for any new issues..."
echo "============================================================"
docker logs --since 5m dev-api-1 2>&1 | grep -E "(ERROR|Connecting to Ray|Connected to Ray)" | tail -20
