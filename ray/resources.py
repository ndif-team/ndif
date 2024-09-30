import argparse
import json

from .util import get_total_cudamemory_MBs


def main(head: bool, name: str = None):
    """
    Generate and print a JSON representation of system resources.

    This function creates a dictionary of resources, including CUDA memory and
    optional flags for head node and custom resource name.

    Args:
        head (bool): If True, adds a "head" resource with value 10.
        name (str, optional): If provided, adds a custom resource with the given name and value 10.

    Returns:
        None. Prints the JSON representation of the resources.
    """
    resources = {}

    if head:
        resources["head"] = 10

    resources["cuda_memory_MB"] = get_total_cudamemory_MBs()

    if name is not None:
        resources[name] = 10

    print(json.dumps(resources))


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="Generate and print system resources as JSON.")
    parser.add_argument("--head", action="store_true", help="Flag to indicate head node")
    parser.add_argument("--name", default=None, help="Custom resource name")
    main(**vars(parser.parse_args()))
