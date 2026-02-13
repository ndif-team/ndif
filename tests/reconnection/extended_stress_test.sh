#!/bin/bash
# Extended stress test: Many cycles to find degradation patterns

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NUM_CYCLES=${1:-10}
RAY_DOWN_TIME=3
RAY_RECOVERY_TIME=20

timestamp() {
    date "+%H:%M:%S"
}

echo "============================================================"
echo "      Extended Stress Test: $NUM_CYCLES cycles"
echo "============================================================"
echo ""
echo "Parameters:"
echo "  - Ray down time: ${RAY_DOWN_TIME}s"
echo "  - Recovery wait: ${RAY_RECOVERY_TIME}s"
echo ""

cd "$SCRIPT_DIR"

# Arrays to track results
declare -a CYCLE_RESULTS
TOTAL_SUCCESS=0
TOTAL_FAILED=0
CONSECUTIVE_FAILURES=0
MAX_CONSECUTIVE_FAILURES=0

# Initial health check
echo "[$(timestamp)] Initial health check..."
RESULT=$(timeout 120 python3 send_requests.py --count 1 2>&1)
if echo "$RESULT" | grep -q "SUCCESS"; then
    echo "[$(timestamp)] ✓ System healthy, starting stress test"
else
    echo "[$(timestamp)] ✗ System not healthy, aborting"
    exit 1
fi

echo ""
echo "============================================================"

for cycle in $(seq 1 $NUM_CYCLES); do
    echo ""
    echo "--- Cycle $cycle/$NUM_CYCLES ---"
    
    # Stop Ray
    docker stop dev-ray-1 > /dev/null 2>&1
    sleep $RAY_DOWN_TIME
    docker start dev-ray-1 > /dev/null 2>&1
    
    # Wait for recovery
    sleep $RAY_RECOVERY_TIME
    
    # Try a request
    START=$(date +%s.%N)
    RESULT=$(timeout 60 python3 send_requests.py --count 1 2>&1)
    END=$(date +%s.%N)
    DURATION=$(echo "$END - $START" | bc)
    
    if echo "$RESULT" | grep -q "1 success"; then
        STATUS="✓ SUCCESS"
        TOTAL_SUCCESS=$((TOTAL_SUCCESS + 1))
        CONSECUTIVE_FAILURES=0
    else
        STATUS="✗ FAILED"
        TOTAL_FAILED=$((TOTAL_FAILED + 1))
        CONSECUTIVE_FAILURES=$((CONSECUTIVE_FAILURES + 1))
        if [ $CONSECUTIVE_FAILURES -gt $MAX_CONSECUTIVE_FAILURES ]; then
            MAX_CONSECUTIVE_FAILURES=$CONSECUTIVE_FAILURES
        fi
    fi
    
    echo "[$(timestamp)] Cycle $cycle: $STATUS (${DURATION}s)"
    CYCLE_RESULTS+=("Cycle $cycle: $STATUS")
    
    # If we have 3+ consecutive failures, something is really broken
    if [ $CONSECUTIVE_FAILURES -ge 3 ]; then
        echo ""
        echo "[$(timestamp)] WARNING: $CONSECUTIVE_FAILURES consecutive failures!"
        echo "[$(timestamp)] Checking API container health..."
        if docker ps | grep -q "dev-api-1"; then
            echo "[$(timestamp)] API container is running"
            echo "[$(timestamp)] Recent API logs:"
            docker logs --tail 20 dev-api-1 2>&1 | grep -E "(ERROR|Connected|Connecting)"
        else
            echo "[$(timestamp)] API container is NOT running!"
        fi
        echo ""
    fi
done

echo ""
echo "============================================================"
echo "      EXTENDED STRESS TEST RESULTS"
echo "============================================================"
echo ""
echo "Total: $TOTAL_SUCCESS success, $TOTAL_FAILED failed out of $NUM_CYCLES cycles"
SUCCESS_RATE=$(echo "scale=1; $TOTAL_SUCCESS * 100 / $NUM_CYCLES" | bc)
echo "Success rate: ${SUCCESS_RATE}%"
echo "Max consecutive failures: $MAX_CONSECUTIVE_FAILURES"
echo ""

# Final health check
echo "[$(timestamp)] Final health check (5 requests)..."
sleep 30  # Give system time to stabilize
RESULT=$(timeout 120 python3 send_requests.py --count 5 2>&1)
FINAL_SUCCESS=$(echo "$RESULT" | grep -o "[0-9]* success" | grep -o "[0-9]*")
echo "[$(timestamp)] Final health: $FINAL_SUCCESS/5 requests succeeded"

echo ""
echo "============================================================"
echo "API Error Summary:"
echo "============================================================"
docker logs --since 15m dev-api-1 2>&1 | grep -c "ERROR" | xargs echo "Total ERROR count:"
echo ""
echo "Error types:"
docker logs --since 15m dev-api-1 2>&1 | grep "ERROR" | grep -oE "\[ERROR\].*" | sort | uniq -c | sort -rn | head -10
