# Refactor Plan: Fix `from_cache()` GPU Reassignment

## Issue Summary (ndif-team/ndif#238)

When a `ModelActor` is evicted and later restored via `from_cache()`, NDIF attempts to reassign it to a different GPU by updating `CUDA_VISIBLE_DEVICES`. But **CUDA ignores runtime changes to this env var** — once the CUDA context is initialized in a process, device visibility is locked. The result: the actor *thinks* it's on the new GPU (e.g., GPU 5) but is actually still using the original GPU (e.g., GPU 1), leading to unexpected GPU co-location and resource conflicts.

## Root Cause

1. Ray sets `CUDA_VISIBLE_DEVICES` when an actor starts, restricting it to 1 GPU (e.g., `CUDA_VISIBLE_DEVICES=1`).
2. PyTorch initializes CUDA, and from that point the actor can only ever see that one physical GPU as `cuda:0`.
3. `from_cache()` updates `os.environ["CUDA_VISIBLE_DEVICES"]` to the new GPU — but CUDA doesn't re-read it. The model stays on the original GPU.

## Proposed Fix: All Devices Visible + `max_memory` Targeting

### Core Idea

Instead of relying on `CUDA_VISIBLE_DEVICES` for isolation, give each `ModelActor` visibility to **all GPUs** and use **HuggingFace Accelerate's `max_memory` parameter** to control which GPU(s) the model actually loads onto.

### Validity Assessment

| Aspect | Assessment |
|--------|-----------|
| **Solves the bug?** | ✅ Yes — with all devices visible, `from_cache()` can move a model to any GPU at any time using `max_memory` or `device_map={"": target_gpu}`, no process restart needed. |
| **CUDA compatibility?** | ✅ Yes — all devices are visible from process start, so `torch.cuda.device(N)` and `.to("cuda:N")` work for any N. |
| **Ray compatibility?** | ⚠️ Requires overriding Ray's default behavior. Ray normally sets `CUDA_VISIBLE_DEVICES` per actor. You need to either: (a) set `num_gpus=0` and manage GPU assignment yourself, or (b) override `CUDA_VISIBLE_DEVICES` in the actor's `__init__` before any CUDA call, or (c) use a Ray runtime env to unset it. |
| **Resource accounting?** | ⚠️ If you set `num_gpus=0` to avoid Ray restricting visibility, Ray no longer tracks GPU usage. NDIF's controller must handle all GPU bookkeeping (which it likely already does). |
| **Isolation risk?** | ⚠️ A buggy actor could accidentally allocate on the wrong GPU. Mitigated by always using `max_memory` with `"0GiB"` for non-target GPUs. |

**Overall: Valid approach**, with caveats around Ray resource tracking and the need for disciplined GPU targeting.

---

## Refactor Plan

### Phase 1: Make All GPUs Visible to ModelActors

**File:** Actor creation / Ray actor decorator configuration

1. Change the Ray actor spec so `ModelActor` sees all GPUs:
   ```python
   # Option A: Set num_gpus=0, manually manage GPU assignment
   @ray.remote(num_gpus=0)
   class ModelActor:
       ...

   # Option B: Override CUDA_VISIBLE_DEVICES in actor __init__ before torch import
   # (Less clean, but works if you need Ray to still track GPU resources)
   ```

2. In the actor's `__init__`, ensure all GPUs are visible:
   ```python
   import os
   # Remove Ray's restriction — must happen BEFORE importing torch
   if "CUDA_VISIBLE_DEVICES" in os.environ:
       del os.environ["CUDA_VISIBLE_DEVICES"]
   # OR: os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3,4,5,6,7"

   import torch  # Now sees all 8 GPUs
   ```

> ⚠️ **Critical:** This env var override must happen before `torch` is imported or any CUDA context is created. Structure the actor's `__init__` accordingly.

### Phase 2: GPU-Targeted Model Loading

**File:** `src/services/ray/src/ray/deployments/modeling/base.py`

Create a helper that loads a model onto a specific GPU using `max_memory`:

```python
import torch
from transformers import AutoModelForCausalLM

def load_model_on_gpu(model_name_or_path, gpu_id, **kwargs):
    """Load a model onto a specific GPU using max_memory targeting."""
    num_gpus = torch.cuda.device_count()
    max_memory = {
        i: (
            f"{torch.cuda.get_device_properties(i).total_mem // (1024**3)}GiB"
            if i == gpu_id
            else "0GiB"
        )
        for i in range(num_gpus)
    }
    return AutoModelForCausalLM.from_pretrained(
        model_name_or_path,
        device_map="auto",
        max_memory=max_memory,
        **kwargs,
    )
```

### Phase 3: Fix `from_cache()` to Use Device Targeting

**File:** `src/services/ray/src/ray/deployments/modeling/base.py` — `from_cache()` method

Instead of updating `CUDA_VISIBLE_DEVICES`, move the model to the target GPU:

```python
def from_cache(self, new_gpu_id: int):
    """Restore model from cache onto a (potentially different) GPU."""

    # 1. Offload current model to CPU / free GPU memory
    self.model.cpu()
    torch.cuda.empty_cache()

    # 2. Reload / move model to the new target GPU
    #    Option A: If model is in CPU cache (state_dict), reload with max_memory
    self.model = load_model_on_gpu(
        self.model_path,
        gpu_id=new_gpu_id,
        torch_dtype=self.dtype,
    )

    #    Option B: If model is already on CPU, just .to() it
    #    self.model.to(f"cuda:{new_gpu_id}")

    # 3. Update internal tracking
    self.cuda_devices = new_gpu_id
    logger.info(f"Model restored from cache onto GPU {new_gpu_id}")
```

### Phase 4: Update Controller GPU Bookkeeping

**File:** Controller / scheduler that manages `ModelActor` placement

Since Ray is no longer tracking GPU assignment (if using `num_gpus=0`), ensure NDIF's controller:

1. Maintains its own mapping of `gpu_id → ModelActor`
2. Prevents double-assignment of a GPU (this may already exist)
3. Updates the mapping when `from_cache()` moves a model

### Phase 5: Cleanup & Safeguards

1. **Remove the dead `os.environ["CUDA_VISIBLE_DEVICES"] = ...` code** from `from_cache()` — it never worked.

2. **Add a safety check** to `load_model_on_gpu` to verify the model landed on the right device:
   ```python
   # After loading, verify placement
   actual_devices = set()
   for param in model.parameters():
       actual_devices.add(param.device)
   assert all(d.index == gpu_id for d in actual_devices), \
       f"Model loaded on wrong devices: {actual_devices}"
   ```

3. **Add integration test** replicating the exact scenario from the issue:
   - Deploy 8 models on 8 GPUs
   - Evict model A from GPU 1
   - Deploy model B on GPU 1
   - Evict model C from GPU 5
   - Restore model A from cache onto GPU 5
   - Verify via `nvidia-smi` / `torch.cuda.memory_allocated(5)` that model A is actually on GPU 5

---

## Alternative Approach: Kill and Respawn Actor

If the `max_memory` approach proves too complex or fragile, a simpler alternative is to **kill the old actor and create a new one** on the target GPU:

- When `from_cache()` is needed on a different GPU, don't reuse the actor
- Terminate the old actor, spawn a new `ModelActor` with `num_gpus=1` (Ray assigns the correct GPU)
- Load the cached model in the new actor's `__init__`

**Tradeoff:** More overhead per GPU switch, but zero risk of CUDA device confusion. Ray handles all GPU bookkeeping naturally.

---

## Recommendation

**Go with the "all devices visible + max_memory" approach** (Phases 1–5) because:
- It avoids actor churn (no kill/respawn overhead)
- It enables fast GPU migration (just move tensors)
- It's consistent with how HuggingFace Accelerate is designed to work
- NDIF's controller already does its own GPU bookkeeping

The key risk to watch: ensure `torch` is never imported before `CUDA_VISIBLE_DEVICES` is unset in the actor. Structure the actor module imports carefully (lazy import torch in `__init__`, not at module level).