# Ray Reconnection Tests

This directory contains scripts to test the API's ability to gracefully handle
Ray backend failures and reconnections.

## The Problem

When the Ray backend goes down (container restart, network issues, etc.), the
API's gRPC connection can enter a corrupted state. The current detection and
recovery logic may not properly detect this and reconnect.

## Test Scenarios

1. **Ray container restart during idle** - No active requests
2. **Ray container restart during active requests** - Requests in flight
3. **Multiple rapid restarts** - Stress test the reconnection logic

## Scripts

### `send_requests.py`
Sends concurrent requests to the API. Can run in continuous mode or send a
fixed number of requests.

```bash
# Send 10 requests
python send_requests.py --count 10

# Run continuously (one request every 2 seconds)
python send_requests.py --continuous --interval 2

# Run with multiple concurrent workers
python send_requests.py --continuous --workers 3
```

### `simulate_ray_restart.sh`
Restarts the Ray container to simulate a backend failure.

```bash
./simulate_ray_restart.sh          # Stop and start
./simulate_ray_restart.sh --kill   # Kill (immediate) and start
```

### `test_reconnection.sh`
Main test script that orchestrates a full reconnection test.

```bash
./test_reconnection.sh
```

### `monitor_logs.sh`
Monitors API container logs for errors.

```bash
./monitor_logs.sh
```

## Running the Tests

1. Start the environment:
   ```bash
   cd /disk/u/jadenfk/wd/ndif
   make ta
   ```

2. Wait for Ray to initialize (~30 seconds)

3. Run the test:
   ```bash
   cd reconnection
   ./test_reconnection.sh
   ```

4. Watch the output for:
   - Requests that succeed before restart
   - Errors during restart
   - Whether requests resume after restart
   - API logs showing reconnection attempts

## Expected Behavior (After Fix)

1. Requests in flight during restart should fail with an error message
2. API should detect the broken connection
3. API should reconnect to Ray
4. New requests should succeed
5. No infinite loops or hangs

## Current Behavior (Bug)

1. `RayProvider.connected()` returns True even when gRPC channel is broken
2. Processors get stuck in BUSY state
3. Requests fail repeatedly without proper reconnection
