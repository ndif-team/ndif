#!/usr/bin/env python3
"""
Test what happens when Ray actually dies while connected.
This is a non-interactive version that controls Ray via subprocess.
Run from the HOST machine (not inside container).
"""

import subprocess
import time
import sys


def run_in_container(code):
    """Run Python code in the API container."""
    result = subprocess.run(
        ["docker", "exec", "dev-api-1", "python3", "-c", code],
        capture_output=True,
        text=True,
    )
    return result.stdout, result.stderr, result.returncode


def stop_ray():
    subprocess.run(["docker", "stop", "dev-ray-1"], capture_output=True)


def start_ray():
    subprocess.run(["docker", "start", "dev-ray-1"], capture_output=True)


def main():
    print("=" * 60)
    print("Testing: What happens when Ray dies during active connection")
    print("=" * 60)

    # Step 1: Connect and store state
    print("\n1. Creating connection in container...")
    code = """
import ray
import pickle
import sys

# Connect
ray.init(address="ray://ray:10001", logging_level="error")
controller = ray.get_actor("Controller", namespace="NDIF")

# Verify it works
result = ray.get(controller.__ray_ready__.remote(), timeout=10)
print(f"Connected and working: {result}")

# DON'T shutdown - leave connection open
print("Connection left open")
"""
    stdout, stderr, rc = run_in_container(code)
    print(stdout)
    if rc != 0:
        print(f"STDERR: {stderr}")
        return

    # Step 2: Stop Ray
    print("\n2. Stopping Ray container...")
    stop_ray()
    time.sleep(3)
    print("   Ray stopped")

    # Step 3: Try to use the connection (should fail and corrupt state)
    print("\n3. Trying to use connection while Ray is down...")
    code = """
import ray

# Ray should still be "initialized" but broken
print(f"ray.is_initialized(): {ray.is_initialized()}")

if ray.is_initialized():
    try:
        controller = ray.get_actor("Controller", namespace="NDIF")
        result = ray.get(controller.__ray_ready__.remote(), timeout=5)
        print(f"Unexpectedly worked: {result}")
    except Exception as e:
        print(f"Expected failure: {type(e).__name__}")
        print(f"Error: {str(e)[:150]}")
"""
    stdout, stderr, rc = run_in_container(code)
    print(stdout)
    if stderr:
        print(f"STDERR (last 200 chars): ...{stderr[-200:]}")

    # Step 4: Start Ray
    print("\n4. Starting Ray container...")
    start_ray()
    print("   Waiting 35 seconds for Ray to initialize...")
    time.sleep(35)

    # Step 5: Try shutdown + init recovery
    print("\n5. Testing shutdown + init recovery...")
    code = """
import ray

print(f"Before recovery - ray.is_initialized(): {ray.is_initialized()}")

# Try shutdown
print("Calling ray.shutdown()...")
ray.shutdown()
print(f"After shutdown - ray.is_initialized(): {ray.is_initialized()}")

# Try init
print("Calling ray.init()...")
try:
    ray.init(address="ray://ray:10001", logging_level="error")
    print(f"After init - ray.is_initialized(): {ray.is_initialized()}")
except Exception as e:
    print(f"ray.init() FAILED: {e}")
    exit(1)

# Try to get controller
print("Getting controller...")
try:
    controller = ray.get_actor("Controller", namespace="NDIF")
    print(f"Got controller: {controller}")
except Exception as e:
    print(f"get_actor FAILED: {e}")
    exit(1)

# Try remote call - THIS IS THE KEY TEST
print("Trying remote call...")
try:
    result = ray.get(controller.__ray_ready__.remote(), timeout=10)
    print(f"SUCCESS! Remote call worked: {result}")
except Exception as e:
    print(f"FAILED! Remote call error: {type(e).__name__}")
    print(f"Error message: {str(e)[:200]}")
    print("")
    print("THIS CONFIRMS: ray.shutdown()+init() does NOT fix the gRPC issue!")

ray.shutdown()
"""
    stdout, stderr, rc = run_in_container(code)
    print(stdout)
    if stderr:
        # Filter out Ray warnings
        important = [
            l
            for l in stderr.split("\n")
            if "Error" in l or "Exception" in l or "Failed" in l
        ]
        if important:
            print("Relevant STDERR:")
            for l in important[:5]:
                print(f"  {l[:150]}")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
