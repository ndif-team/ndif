#!/usr/bin/env python3
from __future__ import annotations

import argparse

from manual_common import (
    DEFAULT_RAY_ADDRESS,
    MODEL_CANDIDATES,
    connect_ray,
    decide_replica_count,
    get_controller,
    get_model_key,
    get_model_size_bytes,
    print_model_sizes,
    print_status_summary,
    print_system_overview,
)


def has_capacity_failure(result: dict) -> bool:
    deploy = result.get("deploy", {})
    statuses = deploy.get("result", {})
    for status in statuses.values():
        if str(status).upper() == "CANT_ACCOMMODATE":
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Scale replicas until capacity is reached")
    parser.add_argument("--model", default="openai-community/gpt2")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--ray-address", default=DEFAULT_RAY_ADDRESS)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=10)
    args = parser.parse_args()

    print_system_overview()
    sizes = print_model_sizes(MODEL_CANDIDATES)
    model_size = sizes.get(args.model) or get_model_size_bytes(args.model)
    _ = decide_replica_count(model_size)

    connect_ray(args.ray_address)
    controller = get_controller()

    model_key = get_model_key(args.model, args.revision)
    print(f"Model key: {model_key}")

    for step in range(1, args.max_steps + 1):
        print(f"Scale step {step}: scale up by {args.step}")
        result_ref = controller.scale_up.remote(
            model_key=model_key, replicas=args.step, dedicated=False
        )
        result = __import__("ray").get(result_ref)
        print(f"Scale result: {result}")
        if has_capacity_failure(result):
            print("Reached capacity (CANT_ACCOMMODATE). Stopping.")
            return 0

    status = __import__("ray").get(controller.status.remote())
    print_status_summary(status)
    print("Reached max steps without capacity failure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
