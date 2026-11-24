import json

import psutil
import torch


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

    if name is not None:

        resources[name] = 10

    print(json.dumps(resources))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")
    parser.add_argument("--name", default=None)
    main(**vars(parser.parse_args()))
