#!/usr/bin/env python3
"""
Single-process test of Ray failure recovery.
Copy this to the container and run it, then stop/start Ray externally.
"""

import ray
import time
import socket


def is_ray_listening(host="ray", port=10001):
    """Check if Ray port is listening."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False


def wait_for_ray_down(timeout=60):
    """Wait for Ray to stop listening."""
    print("   Waiting for Ray to go down...")
    start = time.time()
    while is_ray_listening() and (time.time() - start) < timeout:
        time.sleep(0.5)
    return not is_ray_listening()


def wait_for_ray_up(timeout=120):
    """Wait for Ray to start listening."""
    print("   Waiting for Ray to come up...")
    start = time.time()
    while not is_ray_listening() and (time.time() - start) < timeout:
        time.sleep(1)
    if is_ray_listening():
        print("   Ray is listening, waiting 30s for full initialization...")
        time.sleep(30)
    return is_ray_listening()


def main():
    print("=" * 60)
    print("Test: Ray failure and shutdown+init recovery")
    print("=" * 60)

    # Step 1: Connect
    print("\n1. Initial connection...")
    ray.init(address="ray://ray:10001", logging_level="error")
    controller = ray.get_actor("Controller", namespace="NDIF")
    print(f"   Got controller: {controller}")

    # Step 2: Verify working
    print("\n2. Verifying remote call works...")
    result = ray.get(controller.__ray_ready__.remote(), timeout=10)
    print(f"   Success: {result}")

    # Step 3: Wait for Ray to die
    print("\n3. NOW STOP RAY: docker stop dev-ray-1")
    if not wait_for_ray_down():
        print("   Timeout waiting for Ray to stop")
        return
    print("   Ray is down!")

    # Step 4: Try to use connection while broken
    print("\n4. Trying to use broken connection...")
    try:
        result = ray.get(controller.__ray_ready__.remote(), timeout=5)
        print(f"   Unexpectedly worked: {result}")
    except Exception as e:
        print(f"   Expected failure: {type(e).__name__}")
        error_msg = str(e)
        print(f"   Error snippet: {error_msg[:150]}...")

        # Check if this is the corruption error
        if "disconnected" in error_msg.lower() or "Rendezvous" in error_msg:
            print("   Connection is now corrupted!")

    # Step 5: Wait for Ray to come back
    print("\n5. NOW START RAY: docker start dev-ray-1")
    if not wait_for_ray_up():
        print("   Timeout waiting for Ray to start")
        return
    print("   Ray is back up!")

    # Step 6: Try shutdown + init
    print("\n6. Attempting recovery with ray.shutdown() + ray.init()...")
    print(f"   Before: ray.is_initialized() = {ray.is_initialized()}")

    ray.shutdown()
    print(f"   After shutdown: ray.is_initialized() = {ray.is_initialized()}")

    ray.init(address="ray://ray:10001", logging_level="error")
    print(f"   After init: ray.is_initialized() = {ray.is_initialized()}")

    # Step 7: Get new controller
    print("\n7. Getting new controller handle...")
    try:
        new_controller = ray.get_actor("Controller", namespace="NDIF")
        print(f"   Got new controller: {new_controller}")
    except Exception as e:
        print(f"   FAILED to get controller: {e}")
        return

    # Step 8: THE KEY TEST - does remote call work after recovery?
    print("\n8. THE KEY TEST: Remote call after shutdown+init...")
    try:
        result = ray.get(new_controller.__ray_ready__.remote(), timeout=10)
        print(f"   SUCCESS! Remote call worked: {result}")
        print("\n   CONCLUSION: ray.shutdown()+init() DOES fix the issue")
        print("   The problem must be in HOW the dispatcher does it.")
    except Exception as e:
        print(f"   FAILED! {type(e).__name__}: {str(e)[:200]}")
        print("\n   CONCLUSION: ray.shutdown()+init() does NOT fix the issue")
        print("   The gRPC corruption persists across shutdown/init!")

    ray.shutdown()
    print("\nDone")


if __name__ == "__main__":
    main()
