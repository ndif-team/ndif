#!/bin/bash
# Stress test: Multiple restart cycles to see if something breaks over time

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NUM_CYCLES=${1:-5}
REQUESTS_PER_CYCLE=3
RAY_DOWN_TIME=5
RAY_RECOVERY_TIME=40

timestamp() {
    date "+%H:%M:%S.%3N"
}

echo "============================================================"
echo "      Stress Test: $NUM_CYCLES restart cycles"
echo "============================================================"
echo ""

# Track overall stats
TOTAL_SUCCESS=0
TOTAL_FAILED=0
CYCLE_RESULTS=()

for cycle in $(seq 1 $NUM_CYCLES); do
    echo ""
    echo "============================================================"
    echo "[$(timestamp)] CYCLE $cycle of $NUM_CYCLES"
    echo "============================================================"
    
    # Send some requests first
    echo "[$(timestamp)] Sending $REQUESTS_PER_CYCLE requests before restart..."
    cd "$SCRIPT_DIR"
    RESULT=$(python3 send_requests.py --count $REQUESTS_PER_CYCLE 2>&1)
    BEFORE_SUCCESS=$(echo "$RESULT" | grep -o "[0-9]* success" | head -1 | grep -o "[0-9]*")
    BEFORE_FAILED=$(echo "$RESULT" | grep -o "[0-9]* failed" | head -1 | grep -o "[0-9]*")
    echo "[$(timestamp)] Before restart: $BEFORE_SUCCESS success, $BEFORE_FAILED failed"
    
    # Restart Ray
    echo "[$(timestamp)] Stopping Ray..."
    docker stop dev-ray-1 > /dev/null 2>&1
    
    echo "[$(timestamp)] Waiting ${RAY_DOWN_TIME}s..."
    sleep $RAY_DOWN_TIME
    
    # Try to send a request while Ray is down (should fail quickly ideally)
    echo "[$(timestamp)] Sending request while Ray is down..."
    RESULT=$(timeout 60 python3 send_requests.py --count 1 2>&1)
    if echo "$RESULT" | grep -q "FAILED"; then
        echo "[$(timestamp)] Request correctly failed while Ray was down"
    elif echo "$RESULT" | grep -q "SUCCESS"; then
        echo "[$(timestamp)] WARNING: Request succeeded while Ray was supposed to be down!"
    else
        echo "[$(timestamp)] Request timed out or had unexpected result"
    fi
    
    echo "[$(timestamp)] Starting Ray..."
    docker start dev-ray-1 > /dev/null 2>&1
    
    echo "[$(timestamp)] Waiting ${RAY_RECOVERY_TIME}s for recovery..."
    sleep $RAY_RECOVERY_TIME
    
    # Send requests after restart
    echo "[$(timestamp)] Sending $REQUESTS_PER_CYCLE requests after restart..."
    RESULT=$(python3 send_requests.py --count $REQUESTS_PER_CYCLE 2>&1)
    AFTER_SUCCESS=$(echo "$RESULT" | grep -o "[0-9]* success" | head -1 | grep -o "[0-9]*")
    AFTER_FAILED=$(echo "$RESULT" | grep -o "[0-9]* failed" | head -1 | grep -o "[0-9]*")
    echo "[$(timestamp)] After restart: $AFTER_SUCCESS success, $AFTER_FAILED failed"
    
    # Update totals
    TOTAL_SUCCESS=$((TOTAL_SUCCESS + BEFORE_SUCCESS + AFTER_SUCCESS))
    TOTAL_FAILED=$((TOTAL_FAILED + BEFORE_FAILED + AFTER_FAILED + 1))  # +1 for the during-downtime request
    
    CYCLE_RESULTS+=("Cycle $cycle: Before=$BEFORE_SUCCESS/$REQUESTS_PER_CYCLE, After=$AFTER_SUCCESS/$REQUESTS_PER_CYCLE")
    
    # Check if something broke
    if [ "$AFTER_SUCCESS" -lt "$REQUESTS_PER_CYCLE" ]; then
        echo "[$(timestamp)] WARNING: Not all requests succeeded after restart!"
    fi
    
    # Check API container is still healthy
    if ! docker ps | grep -q "dev-api-1"; then
        echo "[$(timestamp)] ERROR: API container died!"
        break
    fi
done

echo ""
echo "============================================================"
echo "      STRESS TEST COMPLETE"
echo "============================================================"
echo ""
echo "Results by cycle:"
for result in "${CYCLE_RESULTS[@]}"; do
    echo "  $result"
done
echo ""
echo "Total: $TOTAL_SUCCESS success, $TOTAL_FAILED failed"
echo ""

# Check API logs for any concerning errors
echo "Checking API logs for errors..."
docker logs --since 10m dev-api-1 2>&1 | grep -c "ERROR" | xargs echo "Total ERROR lines:"
docker logs --since 10m dev-api-1 2>&1 | grep -E "(Traceback|Exception)" | tail -5
