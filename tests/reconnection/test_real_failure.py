#!/usr/bin/env python3
"""
Test what happens when Ray actually dies while connected.
Run this INSIDE the API container.
"""

import ray
import time
import subprocess
import sys


def test_real_failure():
    print("Test: What happens when Ray server actually dies")
    print("=" * 60)

    # Connect
    print("1. Initial connection...")
    ray.init(address="ray://ray:10001", logging_level="error")
    controller = ray.get_actor("Controller", namespace="NDIF")
    print(f"   Got controller: {controller}")

    # Test call works
    print("2. Initial remote call...")
    result = ray.get(controller.__ray_ready__.remote(), timeout=10)
    print(f"   Success: {result}")

    print("")
    print("=" * 60)
    print("NOW STOP RAY: docker stop dev-ray-1")
    print("Then press Enter to continue...")
    print("=" * 60)
    input()

    # Try to make a call - should fail
    print("3. Trying remote call while Ray is down...")
    try:
        result = ray.get(controller.__ray_ready__.remote(), timeout=10)
        print(f"   Unexpectedly succeeded: {result}")
    except Exception as e:
        print(f"   Expected failure: {type(e).__name__}")
        print(f"   Message: {str(e)[:200]}")

    print("")
    print("=" * 60)
    print("NOW START RAY: docker start dev-ray-1")
    print("Wait 30 seconds, then press Enter...")
    print("=" * 60)
    input()

    # Now try to recover
    print("4. Checking ray.is_initialized()...")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")

    print("5. Calling ray.shutdown()...")
    ray.shutdown()
    print(f"   After shutdown, ray.is_initialized(): {ray.is_initialized()}")

    print("6. Calling ray.init()...")
    try:
        ray.init(address="ray://ray:10001", logging_level="error")
        print(f"   ray.init() succeeded")
        print(f"   ray.is_initialized(): {ray.is_initialized()}")
    except Exception as e:
        print(f"   ray.init() FAILED: {e}")
        return

    print("7. Getting new controller handle...")
    try:
        controller2 = ray.get_actor("Controller", namespace="NDIF")
        print(f"   Got controller: {controller2}")
    except Exception as e:
        print(f"   FAILED: {e}")
        return

    print("8. Trying remote call on NEW handle...")
    try:
        result = ray.get(controller2.__ray_ready__.remote(), timeout=10)
        print(f"   SUCCESS: {result}")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {str(e)[:200]}")
        print("")
        print("   THIS IS THE BUG - shutdown+init didn't fix the connection!")

    print("9. Trying with OLD handle...")
    try:
        result = ray.get(controller.__ray_ready__.remote(), timeout=10)
        print(f"   SUCCESS: {result}")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {str(e)[:100]}")

    ray.shutdown()
    print("Done")


if __name__ == "__main__":
    test_real_failure()
