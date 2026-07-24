---
title: GPU and Memory Gotchas
one_liner: shm_size and Ray's plasma store, the cache percentage that scales CPU RAM rather than GPU memory, what the controller's ledger does and does not know, HOT/WARM/COLD, how dtype defaults cluster-wide but is settable per model, and the eviction rules that refuse a deploy you expected to work.
tags: [gotchas, operating, controller, ray, gpu]
related: [docs/concepts/deployments-and-eviction.md, docs/runbooks/model-oom-on-deploy.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/operating/models-and-deployment.md, docs/reference/env-vars.md, docs/runbooks/deploy-and-pin-a-model.md, docs/gotchas/networking-and-compose.md]
sources: [docker/docker-compose.yml, src/ndif/services/ray/resources.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py]
---

# GPU and Memory Gotchas

## What this covers

Memory is where NDIF's abstractions leak hardest, because three different things
all call themselves "memory" and only one of them is the GPU. Two facts explain
every trap below:

1. **The controller's GPU accounting is a model, not a measurement.** It keeps a
   per-GPU ledger of reserved bytes, sized from a meta-device estimate made
   *before* anything loads, and it never reads the card's real free memory. A
   deploy is refused when the *ledger* says no; a deploy OOMs when the ledger
   said yes and the card disagreed.
2. **Weights live in one of three places, and moving between them costs a
   different resource each time.** GPU memory (HOT), host RAM (WARM), and the
   HuggingFace cache on disk (COLD) are governed by three unrelated knobs.

## `shm_size: 4gb` — Ray's plasma store is `/dev/shm`

```yaml
    # Ray's plasma object store lives in /dev/shm; the docker default (64MB) is
    # far too small. Bump for real workloads.
    shm_size: "4gb"
```

(`docker/docker-compose.yml:254-256`.)

Docker gives a container **64 MB** of `/dev/shm` unless told otherwise. Ray puts
its plasma object store there. At 64 MB Ray either spills every object to disk —
turning object transfer into a disk workload and crawling — or fails outright at
startup with a shared-memory error. Neither failure mentions `shm_size`, which is
what makes it a gotcha.

Two things follow:

- **Running the `ray` service outside this compose file needs the same flag.**
  `docker run --shm-size=4gb`, a Kubernetes `emptyDir` with `medium: Memory`,
  whatever your orchestrator's equivalent is. `just up` gets it for free; nothing
  else does.
- **4 GB is a development floor, not a recommendation.** Size it to the object
  traffic you actually push through Ray. NDIF does not put model weights in
  plasma — those live in the actor's process — but everything Ray passes between
  processes does go through it.

The GPU reservation in the same block (`docker-compose.yml:257-263`,
`driver: nvidia, count: all`) is the compose equivalent of `--gpus all` and needs
the NVIDIA container toolkit on the host; without it the `ray` container fails to
create. Only `ray` gets GPUs.

## `NDIF_MODEL_CACHE_PERCENTAGE` is a CPU-RAM knob

This is the most consequential piece of documentation drift in the repo, because
it is the variable people reach for during a GPU OOM.

```python
    model_cache_percentage: Optional[float] = float(
        os.environ.get("NDIF_MODEL_CACHE_PERCENTAGE", "0.9")
    )
```

(`.../controller/controller.py:546-548`.) The controller multiplies it by the
node's `cpu_memory_bytes` Ray resource — total host RAM, reported by
`psutil.virtual_memory().total` at `ray start`
(`src/ndif/services/ray/resources.py:20-24`) — to get the node's **WARM cache
budget** (`.../cluster/cluster.py:106-109`).

It never touches GPU memory. Lowering it frees nothing on any card; it only makes
the node hold fewer offloaded (WARM) models in host RAM. **The README describes
this variable as GPU memory; the code is authoritative.**

The levers that actually move GPU allocation are `NDIF_DEFAULT_PADDING_FACTOR`
and `NDIF_DEFAULT_PADDING_BIAS` (below), plus adding cards.

## Where the weights actually are

| Level | Weights | Actor process | Serves requests? | Costs |
|---|---|---|---|---|
| `HOT` | on the assigned GPUs | alive | yes | GPU memory from the ledger |
| `WARM` | in host RAM on the same node | alive | no — raises `CachedActorError` | the node's CPU cache budget, plus a process slot |
| `COLD` | in that node's HuggingFace cache only | none | no | disk |

`HOT` and `WARM` are real controller state. `COLD` is synthesized for reporting —
`status()` scans the local HF cache and lists downloaded repos with no live
deployment.

The transitions are actor methods, not restarts.
`to_cache` (`.../modeling/base.py:186`) cancels any in-flight execution, moves the
module to CPU in one pass, lifts the per-process GPU caps, and empties the CUDA
cache. `from_cache` (`:206`) re-applies the caps for the (possibly *different*)
GPUs, recomputes a balanced device map, strips accelerate's previous hooks, and
re-dispatches. The replica keeps its `replica_id` across a HOT→WARM demotion, so
the Ray actor name is stable.

Two traps live here:

- **A WARM model looks undeployed to the queue.** `get_deployment` returns only
  HOT replicas, so a request for a WARM-only model triggers a fresh provision —
  which the placement logic then usually satisfies by promoting the WARM copy
  (`CACHED_AND_FREE`) rather than reloading from disk. The user still watches a
  `PROVISIONING` → `DEPLOYING` cycle for a model that never left the node.
- **A HOT→WARM demotion can fail into a delete.** `Node.evict` only demotes if it
  can make room in the node's CPU cache budget, greedily dropping the smallest
  WARM entries first. If it can't, the replica is removed outright and the actor
  killed — and the next request pays a full disk load. A tight
  `NDIF_MODEL_CACHE_PERCENTAGE` makes this more likely, which is the one way that
  variable indirectly hurts GPU-side latency.

## What the ledger reserves, and what it can't see

`ModelEvaluator` builds the architecture on the **meta device** through nnsight
(no weights downloaded), sums parameter and buffer bytes at the target dtype, and
pads (`.../cluster/evaluator.py:134-138`):

```python
padded_size = math.ceil(
    entry.base_size_in_bytes
    + entry.base_size_in_bytes * effective_padding
    + self.padding_bias
)
```

| Knob | Env var | Default |
|---|---|---|
| proportional headroom | `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` |
| flat headroom | `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) |
| dtype driving `element_size()` | `NDIF_DEFAULT_DTYPE` | `bfloat16` |

That padding is the *entire* budget for activations, KV cache, CUDA workspaces
and the CUDA context. The actor then enforces it twice: accelerate's `max_memory`
map bounds the load (`build_max_memory`, `.../modeling/util.py:151`), and
`set_process_limits` (`:119`) sets a per-process allocator fraction of
`budget / card_total` per device so a runaway request hits its own limit instead
of trampling a co-tenant.

Things that consume real GPU memory and are **absent from the ledger**:

- **The CUDA context**, roughly 400 MiB per process per device
  (`.../modeling/util.py:91-97`). Three actors sharing a card means three
  contexts; the 500 MiB bias covers one.
- **Anything NDIF didn't place.** The ledger starts each GPU at full capacity and
  only ever subtracts NDIF's own placements. A stray training job, or a model
  actor left behind by a killed controller, is invisible. This is the single most
  common reason "the numbers said it fit".
- **Controller restarts.** The ledger is in-memory. A replaced controller rebuilds
  its cluster model with every GPU marked fully free while the surviving detached
  actors still hold their weights.
- **Fragmentation.** Actors run with
  `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
  (`.../cluster/deployment.py:179`) to blunt it, not remove it.

> **The actor sees every GPU on the node.** Deployment sets
> `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1`
> (`.../cluster/deployment.py:175-177`) so Ray does *not* mask
> `CUDA_VISIBLE_DEVICES`; targeting is done entirely by `max_memory` and the
> per-process caps. Device indices in NDIF's logs are therefore real,
> node-global indices — but nothing stops user code inside the block from
> touching a card the replica was not assigned.

### The multi-GPU cliff

`gpus_needed = ceil(size / per_gpu_memory)` (`.../cluster/node.py:397`). A model
that needs one GPU reserves exactly its padded size there, so several models can
share a card. A model that needs **more than one reserves 100% of every GPU it
spans** (`node.py:402-405`).

Sitting 1% over a card's capacity is the most expensive place in the system to
be: you pay two whole GPUs and waste half of them. Lowering padding to squeeze
under the boundary is a real optimization — and raising padding can accidentally
push a model over it.

`per_gpu_memory` is `cuda_memory_bytes // total_gpus` (`cluster.py:103-105`),
computed once when the node first appears. **Nodes with mixed card sizes are
mis-accounted**: every GPU is assumed to be the average.

## dtype defaults cluster-wide but is settable per model

`DeploymentConfig.dtype` exists, the controller pins it to a concrete value
before evaluating so the estimate and the load agree
(`controller.py:121-128`), and the actor honors it. The CLI now passes it
through: `ndif deploy` has a `--dtype` flag, and `load_model_config` forwards
every `DeploymentConfig` field — `checkpoint`, `revision`, `pinned`, `replicas`,
`actor_class`, `trusted`, `dtype`, `padding_factor`,
`execution_timeout_seconds`, `envoy_class`, `model_key` — so a `dtype:` (or
`padding_factor:`) key in `models.yaml` is applied, not dropped.

When you don't set it, `NDIF_DEFAULT_DTYPE` (default `bfloat16`) applies to every
model deployed without an explicit dtype. It is a controller-process variable, so
changing the *default* means restarting the `ray` service; overriding it for one
model does not — pass `--dtype` or a `models.yaml` entry.

The dtype also feeds the actor's autocast: user-created tensors inside a trusted
(in-process) block are autocast to the model's dtype when it is `float16` or
`bfloat16` (`.../modeling/base.py:400-406`). The sandboxed runner does **not**
autocast, so a block that relies on it behaves differently across the
trusted/untrusted fork.

## Eviction: pinning and the age rule

Eviction happens only to make room. Per GPU the controller sorts evictable
occupants by allocated bytes ascending and takes the smallest ones until enough
is freed. Two rules decide what counts as evictable (`.../cluster/node.py:314`):

```python
def evictable(self, deployment: Deployment, pinned: bool) -> bool:
    if deployment.pinned:
        return False
    if (
        not pinned
        and self.minimum_deployment_time_seconds is not None
        and time.time() - deployment.deployed
        < self.minimum_deployment_time_seconds
    ):
        return False
    return True
```

- **Pinned deployments are never evicted by the controller**, for anything. (An
  operator's `ndif evict` still removes them — `pinned` blocks *automatic*
  eviction only.)
- **A deployment younger than `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default
  3600) cannot be evicted to place a non-pinned model.** Deploying something
  *pinned* ignores the age rule.

This is the rule behind the most surprising failure in the system: on a full
cluster, a deploy or an autoscaled request can come back `CANT_ACCOMMODATE`
purely because the current occupant landed 40 minutes ago. Nothing retries — the
request errors and the user tries again later. An hour later the same command
succeeds with no configuration change, which makes it easy to misdiagnose as
flakiness.

Workarounds, in order of preference: deploy with `--pin` (which bypasses the age
rule for *other* deployments' eviction), evict the occupant explicitly, or lower
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` and restart the controller. Lowering it
trades stability for churn — the rule exists so a burst of one-off requests can't
thrash a model that is actively serving.

## Two related deployment surprises

- **`replicas` is additive.** Deploying a model that already has two replicas
  with `replicas=1` gives you three. Shrinking is `ndif evict`'s job.
- **Autoscaling never scales down.** The queue adds up to
  `NDIF_AUTOSCALING_MAX_REPLICAS` (default 3) replicas per model under pressure
  and removes none; those extra replicas hold GPU memory until something needs
  the room badly enough to evict them, or you trim them by hand.

## Quick checks

```bash
# The ledger's view: what NDIF thinks is allocated and free.
ndif status --verbose | jq '.cluster.nodes[] |
  {name, gpus: [.resources.gpu_details[] | {index, available_memory_bytes}]}'

# The card's view. Any gap is memory the ledger cannot see.
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv

# The padded size the controller computed, per model.
just logs ray | grep '=> Model .* size:'

# The knobs currently in force.
ndif status --verbose | jq '.cluster.evaluator | {padding_factor, padding_bias, dtype}'
```

## Related

- [Deployments and eviction](../concepts/deployments-and-eviction.md) — the
  levels, candidate scoring, and eviction policy as a mental model.
- [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) — the full
  symptom-to-fix runbook, including reading each error shape and right-sizing
  padding from the `gpu_mem` metric.
- [Model actor](../developing/model-actor.md) — what the actor does at load,
  `to_cache`, and `from_cache`.
- [Controller internals](../developing/controller-internals.md) — the reconcile
  loop that turns ledger decisions into actor operations.
- [Networking and compose gotchas](networking-and-compose.md) — the other
  compose-level traps.
