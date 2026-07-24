---
title: Model OOM on Deploy
one_liner: A deploy is refused or the actor dies loading weights — how NDIF's GPU accounting decides, how to read the error, what to evict, and how to right-size the estimate.
tags: [runbook, operating, controller, gpu, errors, gotchas]
related: [docs/concepts/deployments-and-eviction.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/gotchas/gpu-and-memory.md, docs/operating/models-and-deployment.md, docs/reference/env-vars.md, docs/runbooks/deploy-and-pin-a-model.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/resources.py, src/ndif/common/metrics.py]
---

# Model OOM on Deploy

## What this covers

A deploy that reports `CANT_ACCOMMODATE`, or one that succeeds on paper and then
dies with a CUDA OOM while loading. Both come from the same root cause, which is
worth stating before any commands:

**NDIF's GPU accounting is a model, not a measurement.** The controller keeps its
own per-GPU ledger of reserved bytes, sized from a meta-device estimate made
*before* anything loads, and it never reads the actual free memory on the card.
The actor separately enforces its slice of that ledger with accelerate's
`max_memory` and a hard per-process allocator cap. A deploy is refused when the
*ledger* says no. A deploy OOMs when the ledger said yes and reality disagreed.

## How the estimate is made

`ModelEvaluator.__call__` (`cluster/evaluator.py:67-145`) does this, memoized per
`model_key`:

1. Build the architecture on the **meta device** through nnsight
   (`Remotable.from_model_key(..., dispatch=False)`) — no weights, no download of
   the weights, just the config and module tree.
2. Sum `nelement() * element_size()` over every parameter **and every buffer** at
   the target dtype → `base_size_in_bytes`.
3. Pad:

```python
padded_size = math.ceil(
    entry.base_size_in_bytes
    + entry.base_size_in_bytes * effective_padding
    + self.padding_bias
)
```

| Knob | Env var | Default | Meaning |
|---|---|---|---|
| `padding_factor` | `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | proportional headroom (15% of weights) |
| `padding_bias` | `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) | flat headroom added once |
| dtype | `NDIF_DEFAULT_DTYPE` | `bfloat16` | drives `element_size()` |

That padding is all the room activations, KV cache, CUDA workspaces, and the CUDA
context get. For a 7B model in bf16 the base is ~14 GB, so padded ≈ 14 GB × 1.15 +
0.5 GB ≈ 16.6 GB. That is the number the ledger reserves.

The cache entry records the dtype and the `trust_remote_code` flag it was computed
under, and re-evaluates if either changes (`evaluator.py:87-92`) — element sizes
differ, and repo code can build a different architecture. The controller pins a
concrete dtype onto every `DeploymentConfig` *before* evaluating so the estimate
and the actor's load can't diverge (`controller.py:124-129`).

> **Note:** `dtype` is settable per deploy — `ndif deploy --dtype` and a `dtype:`
> key in `models.yaml` both set `DeploymentConfig.dtype`. Leave it unset and the
> deploy uses the controller's cluster-wide `NDIF_DEFAULT_DTYPE`.

## How placement decides

Per node, `Node.evaluate` (`cluster/node.py:387-430`):

```python
gpus_needed = self.gpu_resources.required(model_size_in_bytes)   # ceil(size / per_gpu_memory)
if gpus_needed > self.gpu_resources.total:
    return Candidate(CANT_ACCOMMODATE)
per_gpu_bytes = model_size_in_bytes if gpus_needed == 1 else self.gpu_resources.memory_bytes
```

Two consequences worth internalizing:

- **A multi-GPU model claims 100% of every GPU it spans.** If the padded size is
  1.01× a card, `gpus_needed` is 2 and the ledger reserves both cards entirely —
  no sharing, ~half the memory wasted. Sitting just over the boundary is the most
  expensive place to be.
- **`per_gpu_memory` is `cuda_memory_bytes // total_gpus`** (`cluster.py:104`),
  computed once when the node first appears, from `torch.cuda.mem_get_info`'s
  *total* (`resources.py:27-34`). Nodes with mixed card sizes are mis-accounted.

If enough GPUs already have room, the node is `FREE` (or `CACHED_AND_FREE` if it
holds a WARM copy). Otherwise `find_evictions` looks for the cheapest set of
evictable deployments that frees the shortfall, giving `FULL` /
`CACHED_AND_FULL`; if it can't, `CANT_ACCOMMODATE` (`node.py:327-385`). The
cluster picks the best level across nodes and breaks ties randomly
(`cluster.py:204-223`).

A deployment is evictable only if it is **not pinned**, and — when the incoming
deploy is itself unpinned — only if it is older than
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600)
(`node.py:314-325`).

> **`NDIF_MODEL_CACHE_PERCENTAGE` is not a GPU knob.** It scales
> `cpu_memory_bytes` — total host RAM — into the node's **WARM cache budget**
> (`cluster.py:106-109`, `resources.py:5-7`). Lowering it frees nothing on the
> GPU; it only makes the node hold fewer offloaded models. (The README's
> description of this variable is wrong; the code is authoritative.)

## Reading the error

### Refused before anything loaded

```
Deploying 1 model(s)...
  ✗ nnsight.modeling.transformers.TransformersModel:{...}: CANT_ACCOMMODATE: placed 0 of 1 new replicas before the cluster ran out of room.
```

Every message here is emitted verbatim by the controller and echoed by the CLI
(`cli/lib/deploy.py:138-144`):

| Message | Raised at | Means |
|---|---|---|
| `CANT_ACCOMMODATE: placed N of M new replicas…` | `cluster.py:232-236` | no node could fit it, even after evicting everything evictable. Placement stops for that model at the first failure. |
| `No GPU nodes available.` | `cluster.py:220` | the controller has zero nodes with a `GPU` resource — see [add-a-gpu-node](add-a-gpu-node.md). |
| `Could not accommodate any replicas at this time.` | `cluster.py:264` | fallback when no replicas were placed and nothing else set an error. |
| a Python traceback | `cluster.py:171-183` | the **evaluator** failed — the model was never sized. Gated repo (401), unknown architecture, a repo needing `trust_remote_code`, or no network. This is not a memory problem. |

Check the controller's own view of why:

```bash
just logs ray | grep -E 'Analyzing deployment|cannot be deployed|Deploying .* on '
```

```
=> Analyzing deployment of ...TransformersModel:{"repo_id": "meta-llama/Llama-3.1-70B"...} (replica 1/1) with size 162129586995...
=> ...(replica 1/1) cannot be deployed on any node — stopping further attempts for this model
```

That `size` is the padded byte count. Compare it against what the cluster
actually has free:

```bash
ndif status --verbose | jq '.cluster.nodes[] |
  {name, gpus: [.resources.gpu_details[] | {index, free_gb: (.available_memory_bytes/1073741824)}]}'
```

### The actor died while loading

The deploy call returns successfully — placement is bookkeeping — and the failure
shows up later. Three distinct shapes:

**1. `torch.cuda.OutOfMemoryError` in `load_from_disk`.** The controller awaits
the actor's readiness off the main path and, on failure, deletes the actor and
returns its reserved resources to the ledger:

```
Deployment nnsight...TransformersModel:{...} failed during create: <traceback>
```

(`controller.py:298-323`.) Meanwhile the CLI is polling `__ray_ready__` and,
because `max_restarts=-1`, Ray restarts the actor — which reloads and OOMs again.
The CLI eventually reports `initialization timed out` after 300 s
(`cli/lib/models.py:77-97`). A model actor that keeps cycling
`RUNNING`→`UNHEALTHY` in `ndif status` is this loop.

**2. `RuntimeError` from `verify_device_placement`.** More common than a raw OOM,
and much more informative:

```
RuntimeError: 'model.layers.31.mlp.down_proj.weight' is on 'cpu', expected one of CUDA devices [0, 1]
```

The actor loads with `device_map="balanced"` inside a `max_memory` map capped at
its assigned budget (`modeling/base.py:157-166`). When the budget is too small,
accelerate doesn't error — it *offloads the overflow to CPU*. The post-load check
catches that and refuses to serve a half-CPU model
(`modeling/util.py:166-207`). Read it as "the estimate was too low", not as a
placement bug.

**3. OOM during a request, not at load.** The weights fit; the activations
didn't. `set_process_limits` sets a per-process allocator fraction of
`budget / card_total` on each assigned GPU (`modeling/util.py:119-136`), so the
allocator refuses to grow past the padded budget — a runaway request hits
`OutOfMemoryError` inside its own limit instead of trampling a co-tenant model.
The user gets the traceback as a `Status.ERROR` response
(`modeling/base.py:344-354`); the replica stays up and `cleanup()` empties the
cache (`base.py:521-534`). Fix by raising padding, not by restarting anything.

> **Not an OOM, but it looks like one:** `device-side assert triggered` and
> `an illegal memory access was encountered` poison the process's CUDA context
> permanently. `format_error` flags them as fatal and `run()` kills the actor so
> Ray restarts it with a fresh context (`base.py:70-78`, `:511-519`).

## Making room

```bash
# What is holding GPU memory, and can it be moved?
ndif status --json-output | jq '.deployments[] |
  select(.deployment_level=="HOT") |
  {repo_id, replica_id, pinned, gb: (.size_bytes/1073741824)}'
```

In eviction order of preference:

1. **Extra replicas of a hot model.** Autoscaling adds up to
   `NDIF_AUTOSCALING_MAX_REPLICAS` (3) per model under queue pressure and never
   removes them. `ndif evict <checkpoint> --replica <id>` trims one.
2. **Unpinned models nobody is using.** `ndif queue` shows which models have live
   Processors; anything HOT and absent from that list is idle.
3. **WARM replicas.** They hold no GPU memory, but they consume the node's CPU
   cache budget, and a HOT→WARM demotion needs CPU headroom to succeed —
   without it, an evicted replica is dropped outright rather than cached
   (`node.py:277-312`).
4. **Pinned models.** `pinned` stops the *controller's* automatic eviction, not
   yours: `ndif evict` removes a pinned replica like any other
   (see [deploy-and-pin-a-model](deploy-and-pin-a-model.md)).

The controller ledger releases memory as soon as `evict` returns; you do not have
to wait for a sync tick.

## Right-sizing

**Measure first.** The actor records each request's *extra* GPU footprint —
`peak - baseline` per device, on top of the resident weights — as the `gpu_mem`
Influx measurement with fields `baseline_bytes`, `peak_bytes`, `extra_bytes`
(`common/metrics.py:85-123`, `modeling/base.py:330-339`). Chart `extra_bytes` for
the model, take a high percentile, and compare it against
`padding_factor × base + padding_bias`. If the p99 exceeds your padding, the model
is under-provisioned and will OOM under load, not at deploy.

**Then adjust:**

| Situation | Lever |
|---|---|
| Big models starved of activation room | raise `NDIF_DEFAULT_PADDING_FACTOR` (proportional — scales with the model) |
| Small models OOMing on fixed overhead (CUDA context, workspaces) | raise `NDIF_DEFAULT_PADDING_BIAS` |
| Model sits just over a one-GPU boundary and grabs two whole cards | lower padding so it fits one, or accept the second card |
| Nothing fits at all | add capacity — [add-a-gpu-node](add-a-gpu-node.md) |

Both defaults live on the controller and are read at actor construction
(`controller.py:549-554`), so changing them means restarting the `ray` service
(`just restart ray`, or `just ta ray`). Verify with:

```bash
ndif status --verbose | jq '.cluster.evaluator | {padding_factor, padding_bias, dtype}'
```

> **Note:** `DeploymentConfig.padding_factor` supports a per-model override. There
> is no `ndif deploy` flag for it, but `load_model_config` reads a
> `padding_factor:` key from `models.yaml` (`cli/lib/model_config.py`), so a YAML
> deploy can raise padding for just the models that need it. The dashboard's deploy
> form (`dashboard/backend/routers/deploy.py:30`) and calling
> `ndif.cli.lib.deploy.deploy` directly also set it.

## What the ledger does not know

Every item here is real memory on the card that the controller never subtracted:

- **The CUDA context**, roughly 400 MiB per process per device
  (`modeling/util.py:91-97`). Three actors sharing a GPU means three contexts.
  `NDIF_DEFAULT_PADDING_BIAS`'s 500 MiB is what covers this — one actor's worth.
- **Anything not deployed by NDIF.** `available_memory_bytes` starts at the
  card's full capacity (`node.py:44-52`) and is only ever decremented by NDIF's
  own placements. Another process on the GPU — a stray training job, a leftover
  actor from a killed controller — is invisible to the ledger, and it is the
  single most common cause of "the numbers said it fit".
- **Fragmentation.** Actors run with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
  (`cluster/deployment.py:179`) to blunt it, not eliminate it.
- **Controller state after a restart.** The ledger is in-memory. A replaced
  controller actor rebuilds `Cluster.nodes` from scratch with every GPU marked
  fully free, while the surviving detached model actors still hold their weights.
  Until you evict and redeploy, the ledger is over-optimistic by exactly the
  amount those actors hold.

Cross-check with the truth whenever a number looks impossible:

```bash
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
```

## Related

- [docs/concepts/deployments-and-eviction.md](../concepts/deployments-and-eviction.md)
  — the levels, the candidate scoring, the eviction policy.
- [docs/gotchas/gpu-and-memory.md](../gotchas/gpu-and-memory.md) — the sharp
  edges in one place.
- [docs/developing/model-actor.md](../developing/model-actor.md) — what the actor
  does at load and per request.
- [docs/runbooks/deploy-and-pin-a-model.md](deploy-and-pin-a-model.md) — the
  normal path, and what pinning protects.
- [docs/runbooks/debug-a-stuck-request.md](debug-a-stuck-request.md) — when the
  symptom is a hang rather than a failure.
