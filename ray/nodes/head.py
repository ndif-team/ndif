import ray
from util import get_total_cudamemory_MBs

ray.init(
    address=None, resources={"head": 1, "cuda_memory_MB": get_total_cudamemory_MBs()}
)
