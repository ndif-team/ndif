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
    parser = argparse.ArgumentParser(description="Evict replica(s) and verify")
    parser.add_argument("--model", default="openai-community/gpt2")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--ray-address", default=DEFAULT_RAY_ADDRESS)
    parser.add_argument("--replicas", type=int, default=None)
    parser.add_argument("--replica-id", type=int, default=0)
    parser.add_argument("--replica-id-2", type=int, default=None)
    args = parser.parse_args()

    print_system_overview()
    sizes = print_model_sizes(MODEL_CANDIDATES)

    model_size = sizes.get(args.model) or get_model_size_bytes(args.model)
    target_replicas = args.replicas or decide_replica_count(model_size)

    connect_ray(args.ray_address)
    controller = get_controller()

    model_key = get_model_key(args.model, args.revision)
    print(f"Model key: {model_key}")

    existing = controller.get_deployment.remote(model_key)
    existing = __import__("ray").get(existing)

    if existing is None:
        print("Model not deployed; deploying first...")
        result_ref = controller._deploy.remote(
            model_keys=[model_key], dedicated=False, replicas=target_replicas
        )
        result = __import__("ray").get(result_ref)
        print(f"Deploy result: {result.get('result')}")

    status_before = __import__("ray").get(controller.status.remote())
    before_ids = count_hot_replicas(status_before, model_key)
    print_status_summary(status_before)

    replica_keys = [(model_key, args.replica_id)]
    if args.replica_id_2 is not None:
        replica_keys.append((model_key, args.replica_id_2))

    print(f"Evicting replicas: {replica_keys}")
    result_ref = controller.evict.remote(model_keys=[model_key], replica_keys=replica_keys)
    result = __import__("ray").get(result_ref)
    print(f"Evict result: {result}")

    status_after = __import__("ray").get(controller.status.remote())
    after_ids = count_hot_replicas(status_after, model_key)
    print_status_summary(status_after)

    removed = {args.replica_id}
    if args.replica_id_2 is not None:
        removed.add(args.replica_id_2)

    if not removed.issubset(set(before_ids)):
        raise SystemExit(f"Replicas not present before eviction: {sorted(removed)}")

    if set(after_ids) & removed:
        raise SystemExit(
            f"Replica IDs still present after eviction: {sorted(set(after_ids) & removed)}"
        )

    print(f"OK: removed {sorted(removed)}. Before={before_ids} After={after_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
