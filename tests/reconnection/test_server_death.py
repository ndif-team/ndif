#!/usr/bin/env python3
"""
Test shutdown/init after actual Ray server death.
Run in container, manually stop/start Ray externally.
"""

import ray
import time
import socket
from ray.util.client import ray as client_ray
from ray.util.client.common import return_refs


def is_ray_listening(host="ray", port=10001):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
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
    print("Test: client_ray.call_remote after server death + recovery")
    print("=" * 60)

    # Connect
    print("\n1. Initial connection...")
    ray.init(address="ray://ray:10001", logging_level="error")
    controller = ray.get_actor("Controller", namespace="NDIF")

    # Use client_ray.call_remote like submit() does
    print("\n2. Initial call via client_ray.call_remote()...")
    result_ref = client_ray.call_remote(controller.__ray_ready__)
    result = return_refs(result_ref)
    result_val = ray.get(result, timeout=10)
    print(f"   SUCCESS: {result_val}")

    # Wait for server death
    print("\n3. NOW STOP RAY: docker stop dev-ray-1")
    if not wait_for_ray_down():
        print("   Timeout")
        return
    print("   Ray server is DOWN!")

    # Try to make a call - this corrupts the connection
    print("\n4. Corrupting by making a call while server is down...")
    try:
        result_ref = client_ray.call_remote(controller.__ray_ready__)
        result = return_refs(result_ref)
        ray.get(result, timeout=5)
        print("   Unexpectedly worked!")
    except Exception as e:
        print(f"   Expected failure: {type(e).__name__}")
        if "disconnected" in str(e).lower():
            print("   Connection marked as disconnected!")

    # Wait for server recovery
    print("\n5. NOW START RAY: docker start dev-ray-1")
    if not wait_for_ray_up():
        print("   Timeout")
        return
    print("   Ray server is UP!")

    # Try shutdown + init
    print("\n6. Recovery with ray.shutdown() + ray.init()...")
    print("   Calling ray.shutdown()...")
    ray.shutdown()
    print("   Calling ray.init()...")
    ray.init(address="ray://ray:10001", logging_level="error")
    print("   ray.is_initialized():", ray.is_initialized())

    # Get new controller
    print("\n7. Getting new controller...")
    new_controller = ray.get_actor("Controller", namespace="NDIF")
    print(f"   Got: {new_controller}")

    # THE KEY TEST: Does client_ray.call_remote work?
    print("\n8. KEY TEST: client_ray.call_remote() after server death + recovery...")
    try:
        result_ref = client_ray.call_remote(new_controller.__ray_ready__)
        result = return_refs(result_ref)
        result_val = ray.get(result, timeout=10)
        print(f"   SUCCESS: {result_val}")
        print("\n   CONCLUSION: shutdown+init DOES fix server death corruption!")
        print("   The dispatcher issue must be elsewhere.")
    except Exception as e:
        print(f"   FAILED: {type(e).__name__}: {str(e)[:150]}")
        print("\n   CONCLUSION: shutdown+init does NOT fix server death corruption!")
        print("   This is the root cause!")

    ray.shutdown()
    print("\nDone")


if __name__ == "__main__":
    main()
