---
title: Deployments and Eviction
one_liner: What "a model is deployed" means (detached Ray actors holding weights on reserved GPU memory), the HOT/WARM/COLD levels, how the controller accounts for memory, and what it is allowed to throw away.
tags: [concepts, controller, ray]
related: [docs/concepts/request-lifecycle.md, docs/concepts/queue-and-scheduling.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/operating/models-and-deployment.md, docs/runbooks/deploy-and-pin-a-model.md, docs/runbooks/model-oom-on-deploy.md, docs/gotchas/gpu-and-memory.md]
sources: [src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/common/schema/controller.py]
---

# Deployments and Eviction

## What this covers

What the controller means by a deployment, and the rules by which it decides
where a model goes and what has to leave. Two facts frame it:

1. **A deployment is a set of detached Ray actors, one per replica.** Not a Ray
   Serve deployment, not a container — `Deployment.create` calls
   `actor_class.options(name=..., namespace="NDIF", lifetime="detached")` and
   the actor loads the weights in its own worker process
   (`.../cluster/deployment.py:169`). The name is
   `{replica_id}:ModelActor:{model_key}`, which is exactly how the queue looks a
   replica up.
2. **Memory accounting is a model, not a measurement.** The controller maintains
   its own per-GPU ledger of reserved bytes, sized from a meta-device estimate
   *before* anything loads. The actor separately enforces that budget with
   accelerate's `max_memory` and a per-process cap. When those two disagree, you
   get an OOM the ledger never saw coming.

## Deployment levels

| Level | Where the weights are | Actor alive? | Serves requests? |
|---|---|---|---|
| `HOT` | on the assigned GPUs | yes | yes |
| `WARM` | in CPU RAM on the same node | yes | no — raises `CachedActorError` |
| `COLD` | in the node's HuggingFace cache only | no | no |

`HOT` and `WARM` are real controller state (`node.deployments` and `node.cache`).
`COLD` is synthesized for reporting: `status()` scans the local HF cache and
lists any downloaded repo that has no live deployment
(`controller.py:491`).

A `WARM` replica is a live actor whose `to_cache()` moved the module to CPU,
released the per-process GPU caps, and emptied the CUDA cache
(`.../modeling/base.py:186`). It costs CPU RAM and process slots, but bringing it
back (`from_cache`) skips the disk load entirely — that is the point of the tier.

> **Gotcha:** `get_deployment` only returns `HOT` replicas
> (`controller.py:346`). A model with nothing but `WARM` replicas looks
> undeployed to the queue, which then asks for a *new* replica — and the
> placement logic prefers a node that already has it cached, promoting the
> `WARM` one rather than loading from disk.

## Sizing a model

`ModelEvaluator.__call__` (`.../cluster/evaluator.py:67`) builds the model on the
meta device through nnsight (`Remotable.from_model_key(..., dispatch=False)`),
sums parameter and buffer bytes at the target dtype, then pads:

```
padded = ceil(base + base * padding_factor + padding_bias)
```

Defaults: `NDIF_DEFAULT_PADDING_FACTOR=0.15` and
`NDIF_DEFAULT_PADDING_BIAS=524288000` (500 MiB). The padding is what covers
activations, KV cache, and allocator slack — there is no per-request memory
reservation beyond it.

The estimate is memoized per `model_key`, but re-computed when `dtype` or
`trust_remote_code` differs from the cached entry, because both change the
actual footprint. This is also why the controller pins each config's dtype to a
concrete value before evaluating (`controller.py:124`): the number that places
the replica must be the number the actor loads with.

## Placement

For each model in a deploy call (biggest first, `cluster.py:188`), for each
requested replica, every node is scored and the best-scoring nodes are collected;
ties are broken by `random.choice`:

| `CandidateLevel` | Meaning |
|---|---|
| `CACHED_AND_FREE` (1) | node has a WARM copy and enough free GPU memory |
| `FREE` (2) | enough free GPU memory |
| `CACHED_AND_FULL` (3) | WARM copy present, but evictions are required |
| `FULL` (4) | evictions are required |
| `CANT_ACCOMMODATE` (5) | not placeable on this node |

(`CandidateLevel.DEPLOYED = 0` exists in the enum but `Node.evaluate` never
returns it.)

GPU count is `ceil(size / per_gpu_memory)`, or whatever `DeploymentConfig.gpus`
asked for. Each card it lands on is charged its **share** — `ceil(size / count)` —
not all of it, so a replica spanning four cards and using a third of each leaves
the rest usable, and several models can share a GPU whether or not any of them is
multi-card. Among GPUs that fit, `fitting()` sorts least-available-first —
best-fit packing, so small models land on already-partly-used GPUs and leave
whole GPUs free for large ones.

`DeploymentConfig.replicas` is **additive**: deploying a model that already has
two replicas with `replicas=1` gives you three. Shrinking is `evict`'s job.

### `trusted` follows the deployment

`DeploymentConfig.trusted` (`src/ndif/common/schema/controller.py:36`) becomes
the actor's `trust_remote_code` (`controller.py:280`) *and* is passed to the
evaluator, so the size estimate and the real load agree even when repo code
changes the architecture (`cluster.py:169`). The queue sets it from the request
that first provisions the model, and it is sticky for the life of the deployment
— a trusted deployer vouches for every later request routed to it. So with auth
off, where a request is trusted by default, a model provisioned by such a request
deploys with `trust_remote_code=True`. See [Auth and Limits](auth-and-limits.md).

## Eviction

Eviction happens for one reason: to make room. `Node.find_evictions` is called
only when free memory isn't enough. Per GPU it sorts the evictable occupants by
allocated bytes ascending and takes the smallest ones until enough is freed,
then prefers the GPUs whose plans require the fewest evictions
(`node.py:327`). Two rules gate what counts as evictable
(`node.py:314`):

```python
def evictable(self, deployment: Deployment, pinned: bool) -> bool:
    if deployment.pinned:
        return False
    if (not pinned
        and self.minimum_deployment_time_seconds is not None
        and time.time() - deployment.deployed < self.minimum_deployment_time_seconds):
        return False
    return True
```

- **Pinned deployments are never evicted**, by anything.
- **The minimum-deployment-time rule**: a deployment younger than
  `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default `3600`) cannot be evicted *to
  place a non-pinned model*. Deploying something pinned ignores the age rule
  (but still can't touch pinned neighbours).

The practical consequence is the one to remember: on a full cluster, a user
request for a new model can come back `CANT_ACCOMMODATE` purely because the
current occupant was deployed 40 minutes ago. Nothing retries this
automatically — the request errors and the user tries again later.

When a `HOT` replica is evicted it does not simply die. `Node.evict`
(`node.py:250`) releases its GPU reservation, then tries to make room in the
node's CPU cache — greedily dropping the smallest `WARM` entries if needed
(`find_cache_evictions`). If room exists, the replica is demoted to `WARM`
**keeping its `replica_id`**, so the actor name stays stable across the
transition; if not, it is removed outright and the actor is killed.

**Both outcomes are transparent to an in-flight request**, by different routes.
A removed actor makes the queue's Ray call raise `ActorDiedError`. A demotion
cancels the running request in `to_cache()` (`modeling/base.py:191`, before the
weights move) and raises `CachedActorError`. Both land in `EVICTED_ERRORS` and
both re-queue.

Keeping them in step takes care, because the demote path is the one that looks
harmless: it is the *tidier* eviction — the model stays in CPU RAM for a fast
restore — so anything that makes the WARM cache more effective makes that path
more frequent. If it ever stops re-queueing, evictions quietly start destroying
work on a cluster that appears to be behaving well.

The CPU cache budget is the node's total RAM scaled by
`NDIF_MODEL_CACHE_PERCENTAGE` (default `0.9`), read from the `cpu_memory_bytes`
resource that `resources.py` advertises at `ray start`
(`cluster.py:106`).

## Reconcile: build / apply

The controller never mutates Ray directly from a decision. `Cluster.deploy` and
`Cluster.evict` mutate the in-memory node model; the controller then diffs that
model against its last-applied `self.state` — keyed by
`(node_id, model_key, replica_id)` — and turns the delta into actor operations
(`controller.py:169`, `:218`):

- **delete** — `ray.kill(actor, no_restart=True)`
- **cache** (HOT→WARM) — `actor.to_cache.remote()`, awaited *before* anything
  restores, since restores spend the memory it frees
- **from_cache** (WARM→HOT) — `actor.from_cache.remote(gpus)`, monitored
  asynchronously
- **create** — a new actor with `BaseModelDeploymentArgs` (model key, execution
  timeout, dtype, `trust_remote_code`, and the assigned
  `gpu_mem_bytes_by_id`)

A separate `check_nodes` loop re-reads the Ray node list every
`NDIF_CONTROLLER_SYNC_INTERVAL_S` (default 30) so nodes joining or leaving are
picked up; a departed node's deployments are purged.

## What the queue sees

The queue and the controller are only loosely coupled. When an actor disappears
under a replica the dispatch fails, and the queue treats a lookup `ValueError`,
`ActorDiedError`, or `CachedActorError` identically: drop the replica, put the
request back at the front of the line, re-provision if work remains
(`.../queue/replica.py:52`). An out-of-band deploy or evict (CLI, dashboard)
also pushes a `reconcile_model` event so the affected `Processor` picks up any
replica added out-of-band. It does *not* tear down replicas the controller has
dropped — those drop themselves through the `EVICTED_ERRORS` path above, which
re-queues whatever they were running.

> **Gotcha:** `NDIF_MODEL_CACHE_PERCENTAGE` is the fraction of a node's **CPU
> RAM** budgeted for the WARM cache, not GPU memory — the README's one-line
> description says GPU.

## Pinning and boot-time deployments

`NDIF_DEPLOYMENTS` is a `|`-separated list of model keys the controller deploys
**pinned** at startup (`controller.py:90`). Pinning is also available per deploy
call (`DeploymentConfig(pinned=True)`, what `ndif deploy --pin` sends). A pinned
deployment is exempt from eviction and is reported without the `schedule.end_time`
that non-pinned deployments carry in `status()`.

Replicas the *queue* provisions are never pinned — it always sends
`DeploymentConfig(replicas=1, trusted=...)` (`.../queue/replica.py:102`), so
autoscaled capacity is always reclaimable.

## Related

- [Controller internals](../developing/controller-internals.md) — the reconcile
  loop, `Cluster`/`Node`/`Deployment`/`ModelEvaluator` in detail.
- [Models and deployment](../operating/models-and-deployment.md) — actually
  deploying, pinning, revisions, dtype, PEFT.
- [Model actor](../developing/model-actor.md) — what happens inside the actor on
  load, `to_cache`, and `from_cache`.
- [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) — when the estimate
  and reality disagree.
