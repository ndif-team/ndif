import json

import psutil
import torch

from .numa import get_gpu_numa_mapping


def get_available_cpu_memory_bytes():
    mem = psutil.virtual_memory()
    return mem.available


def get_total_cudamemory_bytes(return_ids=False) -> int:
    cudamemory = 0

    ids = []

    for device in range(torch.cuda.device_count()):
        try:
            cudamemory += torch.cuda.mem_get_info(device)[1]
            if return_ids:
                ids.append(device)
        except:
            pass

    if return_ids:
        return int(cudamemory), ids

    return int(cudamemory)


def main(head: bool, name: str = None):
    resources = {}

    if head:
        resources["head"] = 10

    resources["cuda_memory_bytes"] = get_total_cudamemory_bytes()
    resources["cpu_memory_bytes"] = get_available_cpu_memory_bytes()

    # Encode GPU-to-NUMA mapping as individual numeric resources.
    # Ray resources must be numeric, so we use "numa_gpu_<idx>: <node_id + 1>"
    # (offset by 1 so that NUMA node 0 maps to value 1; value 0 would be
    # ignored by Ray as "no resource").
    gpu_count = torch.cuda.device_count()
    gpu_to_numa = get_gpu_numa_mapping(gpu_count)
    for gpu_idx, numa_node_id in gpu_to_numa.items():
        resources[f"numa_gpu_{gpu_idx}"] = numa_node_id + 1

    if name is not None:
        resources[name] = 10

    print(json.dumps(resources))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")
    parser.add_argument("--name", default=None)
    main(**vars(parser.parse_args()))
