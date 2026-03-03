#!/usr/bin/env python3
"""
Test what RayProvider.connected() returns after server death + recovery.
This is to verify if the dispatcher's check is the problem.
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
    print("Test: What does connected() return after server death?")
    print("=" * 60)

    # Connect
    print("\n1. Initial connection...")
    ray.init(address="ray://ray:10001", logging_level="error")

    print(f"\n2. State after initial connection:")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Verify working
    controller = ray.get_actor("Controller", namespace="NDIF")
    result = ray.get(controller.__ray_ready__.remote(), timeout=10)
    print(f"   remote call works: {result}")

    # Wait for server death
    print("\n3. NOW STOP RAY: docker stop dev-ray-1")
    if not wait_for_ray_down():
        print("   Timeout")
        return
    print("   Ray server is DOWN!")

    print(f"\n4. State while server is DOWN:")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Corrupt connection by trying to use it
    print("\n5. Corrupting connection by trying to use it...")
    try:
        result = ray.get(controller.__ray_ready__.remote(), timeout=5)
    except Exception as e:
        print(f"   Expected failure: {type(e).__name__}")

    print(f"\n6. State AFTER corruption (server still down):")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")
    print(f"   connected_like_provider(): {connected_like_provider()}")

    # Wait for server recovery
    print("\n7. NOW START RAY: docker start dev-ray-1")
    if not wait_for_ray_up():
        print("   Timeout")
        return
    print("   Ray server is UP!")

    print(f"\n8. State after server comes back (NO shutdown/init called):")
    print(f"   ray.is_initialized(): {ray.is_initialized()}")
    print(f"   is_ray_listening(): {is_ray_listening()}")

    # THE KEY TEST
    conn_result = connected_like_provider()
    print(f"   connected_like_provider(): {conn_result}")

    if conn_result:
        print("")
        print("   *** PROBLEM FOUND! ***")
        print("   connected() returns TRUE but connection is actually broken!")
        print("   This is why the dispatcher doesn't reconnect!")
        print("")

        # Verify that actual calls fail
        print("   Verifying actual calls fail...")
        try:
            new_controller = ray.get_actor("Controller", namespace="NDIF")
            result = ray.get(new_controller.__ray_ready__.remote(), timeout=10)
            print(f"   Unexpectedly worked: {result}")
        except Exception as e:
            print(f"   Call FAILED as expected: {type(e).__name__}")
            print(f"   Message: {str(e)[:100]}")
    else:
        print("")
        print("   Good: connected() correctly returns False")

    ray.shutdown()
    print("\nDone")


if __name__ == "__main__":
    main()
