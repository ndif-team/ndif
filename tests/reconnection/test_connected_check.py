#!/usr/bin/env python3
"""
Test if connected() returns correct value after Ray failure.
"""

import ray
import time
import socket


def is_ray_listening(host="ray", port=10001):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False


def connected_like_provider():
    """Replicate RayProvider.connected() logic."""
    connected = ray.is_initialized() and is_ray_listening()
    if connected:
        try:
            ray.get_actor("Controller", namespace="NDIF")
        except:
            return False
        else:
            return True
    return False


def wait_for_ray_down(timeout=60):
    print("   Waiting for Ray to go down...")
    start = time.time()
    while is_ray_listening() and (time.time() - start) < timeout:
        time.sleep(0.5)
    return not is_ray_listening()


def wait_for_ray_up(timeout=120):
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
    print("Test: Does connected() lie after Ray failure?")
    print("=" * 60)

    # Connect
    print("\n1. Initial connection...")
    ray.init(address="ray://ray:10001", logging_level="error")

    print(f"\n2. Initial state:")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Get controller and verify working
    controller = ray.get_actor("Controller", namespace="NDIF")
    result = ray.get(controller.__ray_ready__.remote(), timeout=10)
    print(f"   remote call works: {result}")

    # Wait for Ray to die
    print("\n3. NOW STOP RAY: docker stop dev-ray-1")
    if not wait_for_ray_down():
        print("   Timeout")
        return
    print("   Ray is down!")

    print(f"\n4. State while Ray is down:")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Corrupt the connection by trying to use it
    print("\n5. Corrupting connection by trying to use it...")
    try:
        result = ray.get(controller.__ray_ready__.remote(), timeout=5)
    except Exception as e:
        print(f"   Connection corrupted: {type(e).__name__}")

    print(f"\n6. State AFTER corruption:")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Wait for Ray to come back
    print("\n7. NOW START RAY: docker start dev-ray-1")
    if not wait_for_ray_up():
        print("   Timeout")
        return
    print("   Ray is back up!")

    print(f"\n8. State after Ray is back (WITHOUT reset):")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")

    # THE KEY TEST: What does connected() say?
    conn_result = connected_like_provider()
    print(f"   connected_like_provider(): {conn_result}")

    if conn_result:
        print(
            "\n   PROBLEM: connected() returns True but connection is actually broken!"
        )
        print("   This is why the dispatcher doesn't reconnect!")
    else:
        print("\n   Good: connected() correctly returns False")

    # Verify that actual calls still fail
    print("\n9. Testing actual remote call (should fail if connection is corrupted)...")
    try:
        new_controller = ray.get_actor("Controller", namespace="NDIF")
        result = ray.get(new_controller.__ray_ready__.remote(), timeout=10)
        print(f"   Unexpectedly worked: {result}")
    except Exception as e:
        print(f"   FAILED as expected: {type(e).__name__}: {str(e)[:100]}")

    ray.shutdown()
    print("\nDone")


if __name__ == "__main__":
    main()
