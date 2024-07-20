import json

from .util import get_total_cudamemory_MBs

def main(head: bool):

    resources = {}

    if head:

        resources["head"] = 1

    resources["cuda_memory_MB"] = get_total_cudamemory_MBs()
        
    print(json.dumps(resources))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")

    main(**vars(parser.parse_args()))
