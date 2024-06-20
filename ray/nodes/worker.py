import ray
from util import get_total_cudamemory_MBs

ray.init(resources={"cuda_memory_MB": get_total_cudamemory_MBs()})
