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
    parser = argparse.ArgumentParser(description="Evict to free space, then deploy new model")
    parser.add_argument("--model-a", default="openai-community/gpt2")
    parser.add_argument("--revision-a", default="main")
    parser.add_argument("--model-b", default="Qwen/Qwen2.5-1.5B-Instruct")
    parser.add_argument("--revision-b", default="main")
    parser.add_argument("--ray-address", default=DEFAULT_RAY_ADDRESS)
    parser.add_argument("--evict-replica-id", type=int, default=0)
    parser.add_argument("--replicas-b", type=int, default=None)
    args = parser.parse_args()

    print_system_overview()
    sizes = print_model_sizes(MODEL_CANDIDATES)

    size_b = sizes.get(args.model_b) or get_model_size_bytes(args.model_b)
    target_replicas_b = args.replicas_b or decide_replica_count(size_b)

    connect_ray(args.ray_address)
    controller = get_controller()

    model_key_a = get_model_key(args.model_a, args.revision_a)
    model_key_b = get_model_key(args.model_b, args.revision_b)

    print(f"Evicting replica {args.evict_replica_id} from {model_key_a}")
    result_ref = controller.evict.remote(
        model_keys=[model_key_a],
        replica_keys=[(model_key_a, args.evict_replica_id)],
    )
    result = __import__("ray").get(result_ref)
    print(f"Evict result: {result}")

    print(f"Deploying {model_key_b} with replicas={target_replicas_b}")
    result_ref = controller._deploy.remote(
        model_keys=[model_key_b], dedicated=False, replicas=target_replicas_b
    )
    result = __import__("ray").get(result_ref)
    print(f"Deploy result: {result.get('result')}")

    status = __import__("ray").get(controller.status.remote())
    print_status_summary(status)

    replica_ids = count_hot_replicas(status, model_key_b)
    if len(replica_ids) != target_replicas_b:
        raise SystemExit(
            f"Expected {target_replicas_b} HOT replicas for model B, got {replica_ids}"
        )

    print(f"OK: model B replicas = {replica_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
