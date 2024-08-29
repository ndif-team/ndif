import json
from .util import get_total_cudamemory_MBs

def main(head: bool, args):
    
    resources = {}

    if head:

        resources["head"] = 1

    resources["cuda_memory_MB"] = get_total_cudamemory_MBs()
    
    for arg in args:
        
        name, value = arg.split(',')
        value = int(value)
        
        resources[name] = value
        
    print(json.dumps(resources))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--head", action="store_true")
    parser.add_argument('args', nargs=argparse.REMAINDER)
    main(**vars(parser.parse_args()))
