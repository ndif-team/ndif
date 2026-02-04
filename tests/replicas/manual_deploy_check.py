#!/usr/bin/env python3
from __future__ import annotations

import argparse

from manual_common import (
    DEFAULT_RAY_ADDRESS,
    MODEL_CANDIDATES,
    connect_ray,
    count_hot_replicas,
    decide_replica_count,
    get_controller,
    get_model_key,
    get_model_size_bytes,
    print_model_sizes,
    print_status_summary,
    print_system_overview,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy model and verify replicas")
    parser.add_argument("--model", default="openai-community/gpt2")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--ray-address", default=DEFAULT_RAY_ADDRESS)
    parser.add_argument("--dedicated", action="store_true")
    parser.add_argument("--replicas", type=int, default=None)
    args = parser.parse_args()

    print_system_overview()
    sizes = print_model_sizes(MODEL_CANDIDATES)

    model_size = sizes.get(args.model) or get_model_size_bytes(args.model)
    target_replicas = args.replicas or decide_replica_count(model_size)

    connect_ray(args.ray_address)
    controller = get_controller()

    model_key = get_model_key(args.model, args.revision)
    print(f"Model key: {model_key}")
    print(f"Target replicas: {target_replicas}")

    existing = controller.get_deployment.remote(model_key)
    existing = __import__("ray").get(existing)

    if existing is None:
        print("Deploying new model...")
        result_ref = controller._deploy.remote(
            model_keys=[model_key], dedicated=args.dedicated, replicas=target_replicas
        )
        result = __import__("ray").get(result_ref)
        print(f"Deploy result: {result.get('result')}")
        if result.get("evictions"):
            print(f"Evictions: {result['evictions']}")
    else:
        print("Model already deployed; scaling to target replicas...")
        result_ref = controller.scale.remote(
            model_key=model_key, replicas=target_replicas, dedicated=args.dedicated
        )
        result = __import__("ray").get(result_ref)
        print(f"Scale result: {result}")

    status = __import__("ray").get(controller.status.remote())
    print_status_summary(status)

    replica_ids = count_hot_replicas(status, model_key)
    if len(replica_ids) != target_replicas:
        raise SystemExit(
            f"Expected {target_replicas} HOT replicas, got {len(replica_ids)}: {replica_ids}"
        )

    print(f"OK: HOT replicas for {model_key}: {replica_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
