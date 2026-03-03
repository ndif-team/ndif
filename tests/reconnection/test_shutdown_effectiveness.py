#!/usr/bin/env python3
"""
Test whether ray.shutdown() actually fixes the gRPC issue.

This script connects to Ray, simulates a Ray restart,
and tests different reset strategies.
"""

import ray
import time
import subprocess
import sys

RAY_ADDRESS = "ray://localhost:10001"


def check_connection():
    """Test if we can actually make a remote call (not just connect)."""
    try:
        if not ray.is_initialized():
            return False, "Ray not initialized"

        # Try to get the controller
        controller = ray.get_actor("Controller", namespace="NDIF")

        # Try to actually make a remote call (tests the data channel)
        result = ray.get(controller.__ray_ready__.remote(), timeout=10)
        return True, "Data channel working"
    except Exception as e:
        return False, f"Error: {type(e).__name__}: {str(e)[:100]}"


def connect():
    """Connect to Ray."""
    ray.init(address=RAY_ADDRESS, logging_level="error")


def reset_basic():
    """Basic reset - just ray.shutdown()"""
    ray.shutdown()


def reset_with_gc():
    """Reset with garbage collection"""
    import gc

    ray.shutdown()
    gc.collect()
    time.sleep(0.5)


def reset_aggressive():
    """Aggressive reset - try to clear all internal state"""
    import gc

    # Shutdown Ray
    ray.shutdown()

    # Force garbage collection
    gc.collect()
    gc.collect()  # Run twice to catch reference cycles

    # Small delay
    time.sleep(1.0)

    # Try to reset any module-level state
    # Note: This is a hack and may not work
    try:
        import ray._private.client_mode_hook

        if hasattr(ray._private.client_mode_hook, "_client_hook_enabled"):
            ray._private.client_mode_hook._client_hook_enabled = False
    except:
        pass


def test_reset_strategy(name, reset_func):
    """Test a reset strategy after Ray restart."""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"{'='*60}")

    # Step 1: Connect and verify working
    print("1. Initial connection...")
    if ray.is_initialized():
        ray.shutdown()
    connect()
    ok, msg = check_connection()
    print(f"   Status: {'✓' if ok else '✗'} {msg}")

    if not ok:
        print("   ERROR: Initial connection failed, aborting test")
        return

    # Step 2: Stop Ray container
    print("2. Stopping Ray container...")
    subprocess.run(["docker", "stop", "dev-ray-1"], capture_output=True, check=True)
    time.sleep(2)

    # Step 3: Check that connection is now broken
    print("3. Checking connection while Ray is down...")
    ok, msg = check_connection()
    print(f"   Status: {'✓' if ok else '✗'} {msg}")

    # Step 4: Start Ray container
    print("4. Starting Ray container...")
    subprocess.run(["docker", "start", "dev-ray-1"], capture_output=True, check=True)
    print("   Waiting 30 seconds for Ray to initialize...")
    time.sleep(30)

    # Step 5: Apply reset strategy
    print(f"5. Applying reset strategy: {name}")
    reset_func()

    # Step 6: Reconnect
    print("6. Reconnecting to Ray...")
    try:
        connect()
        print("   ray.init() succeeded")
    except Exception as e:
        print(f"   ray.init() failed: {e}")
        return

    # Step 7: Test if data channel works
    print("7. Testing data channel after reset...")
    ok, msg = check_connection()
    print(f"   Status: {'✓' if ok else '✗'} {msg}")

    if ok:
        print(f"\n   ✓ SUCCESS: {name} fixed the gRPC issue!")
    else:
        print(f"\n   ✗ FAILED: {name} did NOT fix the gRPC issue")
        print(f"   The error persists even after shutdown + init")

    return ok


def main():
    print("=" * 60)
    print("Testing if ray.shutdown() fixes the gRPC channel issue")
    print("=" * 60)

    strategies = [
        ("Basic ray.shutdown()", reset_basic),
        ("ray.shutdown() + gc.collect() + 0.5s delay", reset_with_gc),
        ("Aggressive reset (gc + 1s + internal state)", reset_aggressive),
    ]

    results = {}

    for name, func in strategies:
        try:
            results[name] = test_reset_strategy(name, func)
        except Exception as e:
            print(f"\n   ERROR during test: {e}")
            results[name] = False

        # Cleanup for next test
        if ray.is_initialized():
            ray.shutdown()
        time.sleep(2)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for name, success in results.items():
        status = "✓ WORKS" if success else "✗ FAILED"
        print(f"  {status}: {name}")


if __name__ == "__main__":
    main()
