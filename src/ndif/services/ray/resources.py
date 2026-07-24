"""Compute this node's custom Ray resources and print them as JSON.

Shelled out to by start.sh to feed ``ray start --resources``. The controller's
Cluster.update_nodes reads these back:
    cuda_memory_bytes  total GPU memory on this node
    cpu_memory_bytes   total CPU memory (the cache budget, scaled down by the
                       controller's NDIF_MODEL_CACHE_PERCENTAGE)
    head=10            present on the head node (the Controller pins there)
"""

import argparse
import json

import psutil
import torch


def get_total_cpu_memory_bytes() -> int:
    # Total (not available-at-boot) so the advertised cache budget is stable
    # regardless of what else happens to be using RAM when this runs; the
    # controller scales it down by NDIF_MODEL_CACHE_PERCENTAGE.
    return int(psutil.virtual_memory().total)


def get_total_cuda_memory_bytes() -> int:
    total = 0
    for device in range(torch.cuda.device_count()):
        try:
            total += torch.cuda.mem_get_info(device)[1]
        except Exception:
            pass
    return int(total)


def main(head: bool) -> None:
    resources: dict = {}

    if head:
        resources["head"] = 10

    resources["cuda_memory_bytes"] = get_total_cuda_memory_bytes()
    resources["cpu_memory_bytes"] = get_total_cpu_memory_bytes()

    print(json.dumps(resources))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")
    main(**vars(parser.parse_args()))
