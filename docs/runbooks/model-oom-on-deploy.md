---
title: Model OOM on Deploy
one_liner: A deploy is refused or the actor dies loading weights — how NDIF's GPU ledger sizes and places a model, how to read the refusal, what to evict, and how to right-size the padding.
tags: [runbook, operating, controller, gpu, errors, gotchas]
related: [docs/concepts/deployments-and-eviction.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/operating/models-and-deployment.md, docs/reference/env-vars.md, docs/runbooks/deploy-and-pin-a-model.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/resources.py, src/ndif/common/metrics.py, src/ndif/cli/lib/models.py]
---

# Model OOM on Deploy

## What this covers

A deploy that comes back `CANT_ACCOMMODATE` (or another refusal), or one that is
placed and then dies with a CUDA OOM while loading or serving. Both come from one
fact:

**NDIF's GPU accounting is a ledger, not a measurement.** The controller keeps
its own per-GPU count of reserved bytes, sized from an estimate made before
anything loads, and never reads the free memory on the card. The actor then holds
itself to its slice of that ledger with accelerate's `max_memory` and a per-process
allocator cap. A deploy is refused when the ledger says no; it OOMs when the
ledger said yes and the card disagreed.

`ndif` commands below run wherever the CLI can reach `NDIF_RAY_ADDRESS`. On the
compose stack, run them in the `ray` container:

```bash
# a function, not an alias: works in scripts and from any directory (project `dev`)
ndif() { docker exec -i dev-ray-1 ndif "$@"; }
```

## How the size is estimated

`ModelEvaluator` asks nnsight to describe the checkpoint without loading it
(`src/ndif/services/ray/deployments/controller/cluster/evaluator.py`
`ModelEvaluator._entry`). For a HuggingFace repo that is the Hub's published
parameter count times the dtype's bytes per element; a repo with no published
count is built on the meta device and counted instead. Buffers are not in the Hub
count. The result is memoized per `model_key` (which carries `trust_remote_code`), and
recomputed when the dtype changes.

Then it pads (`ModelEvaluator.__call__`):

```
padded = ceil(base + base * padding_factor + padding_bias)
```

| Knob | Env var (controller) | Default | Per-deploy override |
|---|---|---|---|
| `padding_factor` | `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | `--padding-factor`, `padding_factor:` |
| `padding_bias` | `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) | `--padding-bias`, `padding_bias:` |
| dtype | `NDIF_DEFAULT_DTYPE` | `bfloat16` | `--dtype`, `dtype:` |
| base size | — | the estimate | `--size-bytes`, `size_bytes:` (measured weights; padding still applies) |

The padding is all the room activations, KV cache, CUDA workspaces and the CUDA
context get. A 7B model in bf16 is ~14 GB of weights, so the ledger reserves
about 14 × 1.15 + 0.5 ≈ 16.6 GB.

The controller pins a concrete dtype onto every deploy before sizing it, so the
estimate and the actor's load use the same one
(`src/ndif/services/ray/deployments/controller/controller.py`
`_ControllerActor._deploy`). The deployed dtype is reported per replica:
`ndif status --json-output | jq '.deployments[] | {repo_id, dtype}'`. The
`dtype` under `ndif status --verbose`'s `.cluster.evaluator` is torch's process
default (`torch.get_default_dtype()`, set to bfloat16 by the evaluator), not
`NDIF_DEFAULT_DTYPE`; don't read it as the configured default. The dtype also sets
the autocast region every request runs in
(`src/ndif/services/ray/deployments/modeling/nns.py` `request_dtype`).

## How placement decides

Per node, `Node.evaluate`
(`src/ndif/services/ray/deployments/controller/cluster/node.py` `Node.evaluate`):

1. **GPU count.** A deploy that set `gpus` gets exactly that many. Otherwise
   `ceil(padded / per_gpu_memory)`. When tensor parallelism is on
   (`NDIF_TP_MODEL_ACTOR_CLASS` set) and the model shards, a multi-GPU count is
   rounded **up** to the smallest degree the model divides into — a model needing
   3 cards that shards 8 ways takes 4
   (`src/ndif/services/ray/deployments/controller/cluster/evaluator.py` `tp_degree`). `--max-tp 0`
   turns that off for one model.
2. **Too many cards** for the node → `CANT_ACCOMMODATE`.
3. **Per-card charge** is an even share, `ceil(padded / count)`. A model 1.01× a
   card takes two cards at about half each; the rest of each card stays usable.
4. **Room now?** If enough cards have that share free (best fit: fullest card
   first), the node is `FREE`, or `CACHED_AND_FREE` if it holds a WARM copy.
5. **Room after evicting?** `Node.find_evictions` evicts, per card, the smallest
   evictable occupants until the share fits, and takes the cards needing the
   fewest evictions → `FULL` / `CACHED_AND_FULL`. If it can't → `CANT_ACCOMMODATE`.

The cluster takes the best level across nodes, breaking ties at random
(`src/ndif/services/ray/deployments/controller/cluster/cluster.py`
`Cluster.deploy`). Several models in one call are placed biggest first, and the
first replica that can't be placed stops that model.

`per_gpu_memory` is the node's total `cuda_memory_bytes` divided by its GPU count,
read once when the node first appears (`Cluster.update_nodes`;
`src/ndif/services/ray/resources.py` `get_total_cuda_memory_bytes`). Nodes with
mixed card sizes are mis-accounted.

**What can be evicted.** A deployment is evictable unless it is pinned, and —
when the incoming deploy is unpinned — unless it is younger than
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600) (`Node.evictable`). The age
rule causes the most confusing refusal: on a full cluster a deploy, or an
autoscaled request, is refused only because the occupant landed 40 minutes ago,
and the same command succeeds an hour later. Nothing retries it. In order of
preference: deploy with `--pinned` (a pinned deploy ignores the age rule), evict
the occupant yourself, or lower `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (a
controller restart — see [Right-sizing](#right-sizing)), which trades stability for
churn.

> **`NDIF_MODEL_CACHE_PERCENTAGE` is not a GPU knob.** It scales the node's total
> RAM into its WARM-cache budget. Lowering it frees nothing on a GPU.

## Reading the refusal

```
Deploying 1 model(s)...
  ✗ nnsight.modeling.transformers.TransformersModel:{...}: CANT_ACCOMMODATE: placed 0 of 1 new replicas before the cluster ran out of room.
```

The CLI echoes the controller's per-model error verbatim
(`src/ndif/cli/lib/deploy.py` `deploy`). The same text reaches a user whose request
triggered the deploy, as `Could not deploy this model. <reason>`.

| Message | Means |
|---|---|
| `CANT_ACCOMMODATE: placed N of M new replicas…` | no node could fit it, even after evicting everything evictable. The message carries no arithmetic — get the sized bytes from the controller log below |
| `asked for N GPUs, but this model shards at most M ways…` | a node refused for a stated reason: the `gpus` count isn't a degree the model splits into. Pick a divisor of M, or `--max-tp 0` |
| `No GPU nodes available.` | the controller has no node with a `GPU` resource — see [Add a GPU node](add-a-gpu-node.md) |
| a Python traceback | the evaluator failed, so the model was never sized: gated repo (401), unknown architecture, a repo needing `trust_remote_code` that the key does not ask for, no network. Not a memory problem |
| `… asks to run the checkpoint's own code (trust_remote_code), which only a trusted caller may deploy …` | the key asks for repo code and the deploy was untrusted; add `--trusted`. Not a memory problem |

The controller is a Ray actor, so its log is not in `just logs ray`; it is in
Ray's log directory inside the `ray` container:

```bash
docker compose -f docker/docker-compose.yml exec ray \
  bash -c "grep -hE 'Analyzing deployment|cannot be deployed|Deploying .* on ' \
           /tmp/ray/session_latest/logs/worker-*.out"
```

```
=> Analyzing deployment of ...TransformersModel:{"repo_id": "meta-llama/Llama-3.1-70B", ...} (replica 1/1) with size 162129586995...
=> ...(replica 1/1) cannot be deployed on any node — stopping further attempts for this model
```

That `size` is the padded byte count. Compare it with the ledger's free bytes per
card:

```bash
ndif status --verbose | jq '.cluster.nodes[] |
  {name, gpus: [.resources.gpu_details[] | {index, free_gb: (.available_memory_bytes/1073741824)}]}'
```

and with the knobs the controller actually runs with — an override in the `ray`
service's environment is invisible to any page quoting defaults:

```bash
docker compose -f docker/docker-compose.yml exec ray env | grep -E 'NDIF_(DEFAULT|MINIMUM|TP)'
```

## The actor died loading or serving

Placement is bookkeeping, so the deploy call returns before anything loads and
the failure shows up afterwards. Three shapes:

**1. An exception in the constructor — `torch.cuda.OutOfMemoryError`, or the
placement check below.** The controller awaits the actor's readiness off the main
path; on failure it logs `Deployment <model_key> failed during create: <error>`,
kills the actor and returns its bytes to the ledger
(`src/ndif/services/ray/deployments/controller/controller.py`
`_ControllerActor._monitor_deployment`). `ndif deploy` waits on the same actor
with no deadline (`src/ndif/cli/lib/models.py` `wait_for_replica_ready`): it
normally prints `initialization failed - <error>`. If it is still waiting minutes
after the controller log shows `failed during create`, the actor is already gone —
interrupt it and work from the log.

**2. `RuntimeError` from `verify_device_placement`** — more informative than a
raw OOM:

```
RuntimeError: 'model.layers.31.mlp.down_proj.weight' is on 'cpu', expected one of CUDA devices [0, 1]
```

A multi-GPU actor loads with `device_map="balanced"` inside a `max_memory` map
capped at its ledger share
(`src/ndif/services/ray/deployments/modeling/base.py`
`BaseModelDeployment.load_from_disk`). When the share is too small, accelerate
does not error — it offloads the overflow to CPU — and the post-load check
refuses to serve a half-CPU model
(`src/ndif/services/ray/deployments/modeling/util.py` `verify_device_placement`).
Read it as "the estimate was too low". A single-GPU actor is placed on its card
directly and fails as a plain `OutOfMemoryError` instead.

**3. OOM during a request.** The weights fit; the activations didn't.
`set_process_limits` caps the allocator at `share / card_total` on each assigned
card (`src/ndif/services/ray/deployments/modeling/util.py` `set_process_limits`),
so a runaway request hits `OutOfMemoryError` inside its own budget instead of
trampling a co-tenant. The user gets the traceback as an `ERROR`; the replica
stays up and `cleanup()` empties the cache. Fix with padding, not a restart.

The failed request's terminal response carries a `meta` dict
(`src/ndif/services/ray/deployments/modeling/util.py` `request_meta`) — on a
non-blocking job it is in `curl -s localhost:8001/response/<request_id> | jq .meta`;
on a blocking one, ask the user for it.
Its keys:

| Key | Meaning |
|---|---|
| `max_mem_by_gpu` | bytes the request drove on top of the resident weights, per GPU |
| `max_mem_pct_by_gpu` | that, as a percentage of the headroom the request had (share minus weights); 100 means it filled it |
| `alloc_shortfall_by_gpu` | present only on an OOM: how many bytes the refused allocation lacked, per GPU — the amount of padding missing |
| `runtime` | wall-clock seconds |

> **Not an OOM, but it looks like one:** `device-side assert triggered` and
> `an illegal memory access was encountered` poison the process's CUDA context.
> `format_error` marks them fatal and `run()` kills the actor so Ray restarts it
> with a fresh context (`BaseModelDeployment.restart`).

## Making room

```bash
# What is holding GPU memory, and can it be moved?
ndif status --json-output | jq '.deployments[] |
  select(.deployment_level=="HOT") |
  {repo_id, task, replica_id, pinned, gb: (.size_bytes/1073741824)}'
```

In order of preference:

1. **Extra replicas of a hot model.** Autoscaling adds up to
   `NDIF_AUTOSCALING_MAX_REPLICAS` (3) per model under queue pressure and never
   removes them, and `--replicas` on a deploy is additive.
   `ndif evict <checkpoint> --replica <id>` trims one.
2. **Unpinned models nobody is using.** `ndif queue` shows queue depth and the
   executing request per model; a HOT model absent from it, or with an empty
   queue and nothing executing, is idle right now.
3. **WARM replicas.** They hold no GPU memory, but they fill the node's CPU cache
   budget, and a HOT→WARM demotion needs CPU room — without it an evicted replica
   is dropped outright (`Node.evict`).
4. **Pinned models.** `pinned` stops only automatic eviction; `ndif evict` removes
   a pinned replica like any other.

The ledger releases the bytes as soon as `evict` returns.

## Right-sizing

**Measure first.** Each request's extra GPU footprint — peak minus baseline per
device — is the `gpu_mem` Influx measurement, fields `baseline_bytes`,
`peak_bytes`, `extra_bytes` (`src/ndif/common/metrics.py` `GPUMemMetric`). Chart
`extra_bytes` for the model, take a high percentile, and compare it with
`padding_factor × base + padding_bias`. A p99 above the padding means the model
will OOM under load.

| Situation | Lever |
|---|---|
| Big models starved of activation room | raise `NDIF_DEFAULT_PADDING_FACTOR` (scales with the model) |
| Small models OOMing on fixed overhead (CUDA context, workspaces) | raise `NDIF_DEFAULT_PADDING_BIAS` |
| One model needs more than the rest | `ndif deploy --padding-factor 0.3 <checkpoint>` (also `--padding-bias`, `--size-bytes`, `--gpus`, `--max-tp`), or the same keys in `models.yaml`. The dashboard's deploy form takes `padding_factor` |
| Model sits just over a one-card boundary | lower padding so it fits one card, or accept the second |
| Nothing fits | add capacity — [Add a GPU node](add-a-gpu-node.md) |

The `NDIF_DEFAULT_*` and `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` values are read
once, when the controller process imports its module
(`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`), so changing one means restarting the controller.
On the compose stack, set it in the `ray` service's `environment:` and
**recreate** the container:

```bash
just up ray
```

`docker compose restart` (`just restart ray`) keeps the old environment, and
`just ta ray` runs `just down` first and so tears down the whole stack. Recreating
`ray` takes down the controller **and every model actor**: in-flight and queued
requests are errored once the API notices the lost connection, and only
`NDIF_DEPLOYMENTS` models (and dashboard-scheduled ones, on the next reconcile)
come back on their own. Plan it as an outage. Right after the recreate, the first
request errors with `Error starting model. Please try again later.` — the API is
still holding its old compute connection, and its log shows a `ConnectionError` —
and the next may get a `503` until the API reconnects; retry after that.
On a from-source install, change the
variable in the environment `ndif start` runs under, then `ndif stop ray` and
`ndif start ray`.

Verify the controller picked it up:

```bash
ndif status --verbose | jq '{default_padding_factor, default_padding_bias, minimum_deployment_time_seconds}'
```

## What the ledger does not know

Real memory on the card that the controller never subtracted:

- **The CUDA context**, roughly 400 MiB per process per device. Three actors on
  one card are three contexts; the default 500 MiB `padding_bias` covers one.
- **Anything NDIF didn't deploy.** A card's free bytes start at its full capacity
  and only NDIF's own placements reduce them. A stray process on the GPU — another
  job, an orphaned actor from a replaced controller — is invisible, and is the
  most common cause of "the numbers said it fit".
- **Fragmentation.** Actors run with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
  to blunt it.
- **User code on cards the replica wasn't granted.** Actors start with
  `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1`
  (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
  `Deployment.create`), so they see every GPU on the node and device indices in
  logs are node-global. Only `max_memory` and the per-process caps target the
  assigned cards.
- **Controller state after a restart.** The ledger is in memory. A replaced
  controller rebuilds its nodes with every GPU free, while detached model actors
  from before still hold their weights and are not re-adopted. Until those actors
  are killed, the ledger over-counts free memory by exactly what they hold.

Cross-check with the truth whenever a number looks impossible:

```bash
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv
```

## Related

- [Deployments and eviction](../concepts/deployments-and-eviction.md) — the
  levels, candidate scoring and eviction policy as a mental model.
- [Model actor](../developing/model-actor.md) — what the actor does at load and
  per request.
- [Deploy and pin a model](deploy-and-pin-a-model.md) — the normal path.
- [Debug a stuck request](debug-a-stuck-request.md) — when the symptom is a hang.
