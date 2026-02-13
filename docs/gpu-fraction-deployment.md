# GPU Fraction Deployment

## Purpose

Allow multiple small models to share a single GPU by allocating only the memory fraction each model needs, rather than reserving entire GPUs.

## Memory Estimation and Allocation Decision

In `Node.evaluate()` (`src/services/ray/src/ray/deployments/controller/cluster/node.py`), a two-level heuristic decides the allocation strategy per replica:

```python
gpu_fraction_factor = 3.0           # Safety multiplier (CUDA context, KV cache, etc.)
fraction_largest_possible = 0.8     # Max fraction considered for shared allocation
required_bytes = model_size_in_bytes * gpu_fraction_factor
fraction_threshold = gpu_memory_bytes * fraction_largest_possible
single_gpu_fit = model_size_in_bytes <= gpu_memory_bytes
```

### Decision tree

```
Is model already DEPLOYED on this node?
  → Yes: return DEPLOYED (no action needed)

Does model_size fit on a single GPU?
  → Yes:
      Is required_bytes < fraction_threshold (i.e., model_size * 3 < gpu_memory * 0.8)?
        → Yes: FRACTIONAL allocation — reserve only required_bytes on one GPU
        → No:  FULL SINGLE-GPU allocation — reserve the entire GPU
      Call evictions_for_fractional_gpu_memory() to find a GPU
        → Found: return FREE or FULL (depending on whether evictions are needed)
        → Not found: fall through to multi-GPU path

  → No (model too large for one GPU):
      gpus_required = ceil(model_size / gpu_memory) + 1
      Are enough GPUs available?
        → Yes: return FREE with multi-GPU assignment
        → No but enough total GPUs exist:
            Can we evict non-dedicated deployments to free enough?
              → Yes: return FULL with eviction list
              → No: return CANT_ACCOMMODATE
        → No (model exceeds node capacity): return CANT_ACCOMMODATE
```

## Finding a GPU for Fractional Allocation

`evictions_for_fractional_gpu_memory(required_bytes, dedicated)`:
1. Shuffles available GPU IDs (avoids bias toward lower-numbered GPUs)
2. For each GPU: if `available_bytes >= required_bytes`, returns immediately (no evictions needed)
3. Otherwise: computes which evictable deployments on that GPU could be removed to free enough memory
4. Tracks the GPU requiring the fewest evictions across all candidates
5. Returns `(gpu_id, evictions_list)` or `None` if no GPU can accommodate the model

## Resource Tracking

`Resources` dataclass (`node.py`) tracks per-GPU available memory:

- `gpu_memory_available_bytes_by_id: Dict[int, int]` — remaining bytes per physical GPU
- `available_gpus: list[int]` — GPUs with >= 30% memory free (`min_available_gpu_fraction = 0.3`)
- `assign_memory(required_bytes, gpu_id)` — deducts bytes from a specific GPU; if no `gpu_id` given, picks the GPU with the most free memory among those with enough capacity
- `assign_full_gpus(count)` — claims N entire GPUs from `available_gpus`, zeroing their available memory
- `_update_available_gpus(gpu_id)` — re-evaluates whether a GPU stays in the "available" set after any allocation or deallocation

On eviction, memory is returned and GPUs may re-enter the available set:
```python
for gpu_id, bytes_used in deployment.gpu_mem_bytes_by_id.items():
    resources.gpu_memory_available_bytes_by_id[gpu_id] += bytes_used
    resources._update_available_gpus(gpu_id)
```

## GPU Fraction Computation

In `Node.deploy()`, after assigning memory, if the deployment uses exactly one GPU:
```python
gpu_memory_fraction = gpu_mem_bytes_by_id[gpu_id] / gpu_memory_bytes
# Clamped to [0.01, 0.99]
```

For multi-GPU deployments, `gpu_memory_fraction` is `None` (each GPU is fully reserved).

This fraction is passed through `Deployment` → `BaseModelDeploymentArgs` → `ModelActor.__init__` → `torch.cuda.set_per_process_memory_fraction()`.

## Memory Isolation in ModelActor

In `BaseModelDeployment.__init__()` (`src/services/ray/src/ray/deployments/modeling/base.py`):

1. **Device targeting**: `torch.cuda.set_device(first_target_gpu)` — ensures the CUDA context (~400 MiB) lands on the target GPU, not always GPU 0
2. **Memory cap**: `torch.cuda.set_per_process_memory_fraction(fraction, device)` — caps PyTorch allocations at the assigned fraction. If the process exceeds this, the PyTorch allocator raises OOM
3. **`max_memory` dict**: built by `_build_max_memory()` — maps non-target GPUs to 0 bytes and target GPUs to their allocated cap. Passed to `accelerate`'s `dispatch_model` / `from_model_key` to constrain weight placement

`_verify_device_placement(module, source)`: after loading (from disk or cache), logs actual device placement and warns if parameters ended up on unexpected GPUs.

### Cache transitions

- **`to_cache()`**: cancels in-flight work, removes accelerate hooks, moves model to CPU, resets memory fraction to `1.0` per GPU (unlocks the cap), clears CUDA cache, sets `self.cached = True`
- **`from_cache(gpu_mem_bytes_by_id)`**: updates `self.gpu_mem_bytes_by_id` with the new GPU targets, sets device and memory fraction caps, rebuilds `max_memory`, re-dispatches model via `accelerate.dispatch_model`, sets `self.cached = False`

Note: while `self.cached = True`, `ModelActor.__call__` raises `LookupError("Failed to look up actor")` — this causes the Processor to treat the replica as evicted and stop routing requests to it.

## Deployment Record

Each `Deployment` (`src/services/ray/src/ray/deployments/controller/cluster/deployment.py`) stores the GPU allocation:
- `gpu_mem_bytes_by_id: Dict[int, int]` — per-GPU byte allocation (determines `max_memory` and device targeting)
- `gpu_memory_fraction: float | None` — passed to `torch.cuda.set_per_process_memory_fraction`
- `gpus` property — `list(gpu_mem_bytes_by_id.keys())`

Actor lifecycle methods delegate to the Ray actor:
- `create(node_name, args)` — spawns `ModelActor` with `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1` (lets the actor see all GPUs; targeting is done via `max_memory`)
- `cache()` → `actor.to_cache.remote()`
- `from_cache()` → `actor.from_cache.remote(self.gpu_mem_bytes_by_id)` (passes the *new* GPU allocation on restore)
- `delete()` → `ray.kill(actor, no_restart=True)`

## Known Limitations

- **Memory estimation**: the 3x safety factor is a heuristic; inference workloads with large KV caches may OOM
- **No performance isolation evaluation**: co-located models on shared GPUs have not been benchmarked for interference
- **Fraction not configurable per-model**: the heuristic is global; a per-model override may be needed for models with unusual memory profiles
