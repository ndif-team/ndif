import json

from .util import get_total_cudamemory_MBs


def main(head: bool, name: str = None):

    resources = {}

    if head:

        resources["head"] = 10

    resources["cuda_memory_MB"] = get_total_cudamemory_MBs()

    if name is not None:

        resources[name] = 10

    print(json.dumps(resources))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")
    parser.add_argument("--name", default=None)
    main(**vars(parser.parse_args()))
