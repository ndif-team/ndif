# Ray Reconnection Test Findings

## Test Results Summary

| Test | Description | Result |
|------|-------------|--------|
| Stress Test (3 cycles, 40s recovery) | Standard restart cycles | 100% success |
| Aggressive Test (partial recovery) | Request during partial recovery | FAILED |
| Aggressive Test (concurrent recovery) | 3 concurrent requests during recovery | 100% success |
| Aggressive Test (rapid flapping) | 5s recovery cycles | ~50% success |
| Extended Stress Test (20s recovery) | 10 rapid cycles | 0% success (critical) |
| Long Recovery Test (45s recovery) | Single restart with long wait | 33% success |
| **With Reactive Fix (concurrent)** | Stop Ray, start, send 2 concurrent | 1 failed, 1 success ✓ |
| **With Reactive Fix (sequential)** | 5 cycles: fail→succeed pattern | 100% consistent ✓ |
| **Post-reconnect stability** | 1 fail + 5 sequential requests | All 5 succeed ✓ |

## Key Discovery: `ray.shutdown()` + `ray.init()` DOES Work!

After extensive isolated testing, we confirmed that `ray.shutdown()` + `ray.init()` 
**fully resets the gRPC connection**, even after actual Ray server death.

### Proof from isolated test:
```
1. Connect to Ray, make remote call → SUCCESS
2. Stop Ray container (server dies)
3. Try remote call → FAILS (connection corrupted)
4. Start Ray container
5. Call ray.shutdown() + ray.init()
6. Make remote call → SUCCESS!
```

The fix works. The problem was **when** it was being called.

## The Real Problem: Detection Timing

### Original Issue

The dispatcher's `RayProvider.connected()` would return `True` even when the 
connection was broken because:
- `ray.is_initialized()` → True (from old connection)
- `ray.get_actor()` → works (control plane still functional)
- But **data channel is corrupted** → actual remote calls fail

### The Symptom

```
[INFO] Connecting to Ray
[INFO] Connected to Ray           <-- ray.init() succeeded
[ERROR] Request can't be sent...  <-- But data channel is dead!
```

Even though reconnection happened, `connected()` returned `True` before errors 
triggered `handle_errors()`, so requests were dispatched to a broken connection.

## Implemented Fix: Reactive Error Detection (dispatcher.py + ray.py)

**Chosen approach**: Reactive detection with pattern matching on error messages.
This avoids slow remote calls in the hot path while still triggering reconnection.

### Changes to `RayProvider` (ray.py)

Added connection error pattern detection:

```python
CONNECTION_ERROR_PATTERNS = (
    "Ray client has already been disconnected",
    "Unrecoverable error in data channel",
    "_MultiThreadedRendezvous",
    "Failed to reconnect",
    "gRPC",
    "ActorUnavailableError",
)

@classmethod
def is_connection_error(cls, error: Exception) -> bool:
    """Check if an exception indicates a broken Ray connection."""
    error_str = str(error)
    return any(pattern in error_str for pattern in cls.CONNECTION_ERROR_PATTERNS)
```

### Changes to `handle_errors()` (dispatcher.py)

Now checks for connection errors and forces reconnection:

```python
async def handle_errors(self):
    if not self.error_queue.empty():
        # Collect all errors
        errors = []
        while not self.error_queue.empty():
            errors.append(self.error_queue.get_nowait())

        # Check if any are connection errors
        has_connection_error = any(
            RayProvider.is_connection_error(error) for _, error in errors
        )

        if has_connection_error:
            self.logger.warning(
                f"Connection error detected (has_connection_error={has_connection_error}), "
                "forcing reconnection..."
            )
            self.purge("Connection lost to compute backend. Reconnecting. Please retry.")
            await self.redis_client.delete("env")
            self.connect()  # Includes RayProvider.reset() + ray.init()
        
        # Log all errors
        for model_key, error in errors:
            # ... existing error logging ...
```

### Test Result

```
$ docker stop dev-ray-1 && sleep 3 && docker start dev-ray-1 && sleep 35 && python3 send_requests.py --count 2
```

**Log output:**
```
Connection error detected (has_connection_error=True), forcing reconnection...
Connecting to Ray
Connected to Ray
```

**Result:** 1 failed, 1 success
- First request fails (triggers reconnection)
- Second request succeeds (connection now healthy)

### Trade-offs

| Approach | Pros | Cons |
|----------|------|------|
| **Reactive (implemented)** | No latency in hot path | First request after restart fails |
| Proactive (before dispatch) | All requests succeed | 5s remote call blocks every dispatch cycle |
| Background health monitor | Balance of both | Complexity, race conditions |

The reactive approach was chosen because:
1. User explicitly requested avoiding slow checks in the hot path
2. First-request failure is acceptable (user can retry)
3. Subsequent requests all succeed

## Alternative Fixes (Not Implemented)

### Proactive Connection Verification

Verify the connection BEFORE dispatching:

```python
def verify_connection(self) -> bool:
    """Verify the data channel is actually working."""
    if not RayProvider.connected():
        return False
    try:
        controller = controller_handle()
        ray.get(controller.__ray_ready__.remote(), timeout=5)
        return True
    except Exception:
        return False
```

**Note:** This adds ~5s latency to every dispatch cycle when Ray is unhealthy.

### Background Health Monitor

```python
async def health_monitor(self):
    while True:
        await asyncio.sleep(10)  # Check every 10 seconds
        if not self.verify_connection():
            self.logger.warning("Connection health check failed, reconnecting...")
            self.purge("Connection lost. Reconnecting...")
            self.connect()
```

### Retry Failed Requests in Processor

```python
async def execute(self, request):
    for attempt in range(3):
        try:
            result = await submit(self.handle, "__call__", request)
            return result
        except Exception as e:
            if RayProvider.is_connection_error(e) and attempt < 2:
                self.error_queue.put_nowait((self.model_key, e))
                await asyncio.sleep(5)  # Wait for reconnection
                continue
            raise
```

## Multi-Node Scenario Considerations

The real production scenario involves:
1. Head node with Controller actor
2. Compute nodes with ModelActor deployments
3. Compute nodes going down → Head node restarted → Compute nodes slowly reconnect

### Additional Test Cases Needed

1. **Head restart with fresh state**: All old actor handles become invalid
2. **Partial cluster**: Head up, compute nodes not ready
3. **Rolling recovery**: Nodes coming up one by one

### Current Docker Setup Limitation

The `dev-ray-1` container runs both head and workers. To simulate multi-node:
- Would need to add separate containers for head vs workers
- Or use Ray's actual multi-node setup

## Conclusion

**Root Cause:** The connection was broken but `connected()` returned `True` because
it only checked control plane connectivity, not data channel health.

**Solution:** Pattern-match on error messages to detect connection failures, then
force a full `ray.shutdown()` + `ray.init()` cycle.

**Result:** The fix works. First request after Ray restart fails (expected with 
reactive approach), but it triggers reconnection so all subsequent requests succeed.
