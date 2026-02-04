# GPU Fraction Deployment Support

Introduces how GPU fraction deployment is supported in this version.

## What a GPU Fraction Deployment Is

A GPU fraction deployment is a deployment that uses a fraction of the available GPUs.
The gpu memory usage is estimated in `src/services/ray/src/ray/deployments/controller/cluster/node.py` by the following formula:

## Memory Estimation
```python
def gpu_memory_required_bytes(self, model_size_in_bytes: int) -> int:
    return int(model_size_in_bytes * self.gpu_memory_coefficient + self.gpu_memory_buffer_bytes)
```
- `model_size_in_bytes` is the size of the model in bytes
- `self.gpu_memory_coefficient` is a coefficient that is used to estimate reserved gpu memory for the model, it includes the reserved gpu memory for CUDA allocations, context, KVCache, etc.
- `self.gpu_memory_buffer_bytes` is a buffer to avoid corner cases.

## GPU Allocation
When deploying a model, the GPU will be allocated with fraction, the fraction is computed by dividing the estimated memory required by the total memory of the GPU.
If a GPU has less than 30% (configured in `src/services/ray/src/ray/deployments/controller/cluster/node.py` by `min_available_gpu_fraction` in class `Resources`) of the memory available, the GPU will be removed from the available GPUs list. 
Available memory of one gpu is maintained by node resources.

## Memory Isolation
We use `torch.cuda.set_per_process_memory_fraction` to isolate the memory of the model from the other processes running on the GPU. This is done in `src/services/ray/src/ray/deployments/modeling/base.py`.
Note that this is a cap for one process, if the process allocates more memory than the fraction, the PyTorch allocator will raise OOM error.

## TODOs
- The benefit of GPU fraction deployment only excels when the model is small, so we can host multiple models on one GPU with GPU fraction deployment. But we have not evaluated the performance interference between models.
- The memory estimation is not perfect, the inference will be easier to OOM without restrictions.
- We might make the fraction configurable for a model deployment.



