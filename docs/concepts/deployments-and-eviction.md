---
title: Deployments and Eviction
one_liner: What "a model is deployed" means (detached Ray actors holding weights on reserved GPU memory), the HOT/WARM/COLD levels, how the controller sizes and places a model, and what it is allowed to throw away.
tags: [concepts, controller, ray]
related: [docs/concepts/request-lifecycle.md, docs/concepts/queue-and-scheduling.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/operating/models-and-deployment.md, docs/runbooks/deploy-and-pin-a-model.md, docs/runbooks/model-oom-on-deploy.md]
sources: [src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/common/schema/controller.py]
---

# Deployments and Eviction

## What this covers

What the controller means by a deployment, and the rules by which it decides
where a model goes and what has to leave. The code walk is
[Controller internals](../developing/controller-internals.md). Three facts frame
it:

1. **A deployment is a set of detached Ray actors, one per replica.** Not a Ray
   Serve deployment, not a container — `Deployment.create`
   (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`) starts
   a named, detached actor in the `NDIF` namespace that loads the weights in its
   own worker process. The name is `{replica_id}:ModelActor:{model_key}`, which
   is exactly how the queue looks a replica up.
2. **A deployment is keyed by the exact `model_key` string.** The client builds
   it as `"<import.path.ClassName>:<model id>"`, and for a `TransformersModel` the
   id carries the repo, revision and resolved task. So a key written out by hand
   — an `NDIF_DEPLOYMENTS` entry, or a verbatim `model_key` in `models.yaml` —
   must carry the task clients resolve; one that differs deploys a model no
   request routes to. `ndif deploy <checkpoint>`, a `models.yaml` entry without
   `task:`, and the dashboard's deploy form all infer the task as the client
   does.
3. **Memory accounting is a model, not a measurement.** The controller keeps its
   own per-GPU ledger of reserved bytes, sized from an estimate *before* anything
   loads. The actor separately enforces that budget with accelerate's
   `max_memory` and a per-process cap. When those two disagree, you get an OOM
   the ledger never saw coming.

## Deployment levels

| Level | Where the weights are | Actor alive? | Serves requests? |
|---|---|---|---|
| `HOT` | on the assigned GPUs | yes | yes |
| `WARM` | in CPU RAM on the same node | yes | no — raises `CachedActorError` |
| `COLD` | in the node's HuggingFace cache only | no | no |

`HOT` and `WARM` are real controller state. `COLD` is synthesized for reporting:
`_ControllerActor.status` lists any downloaded repo with no live deployment.

A `WARM` replica is a live actor whose `to_cache()` moved the module to CPU and
released its GPU memory. Bringing it back (`from_cache`) skips the disk load —
the point of the tier. Moving between levels is an actor method, not a restart:
the replica keeps its `replica_id`, so the Ray actor name is stable.

> **Gotcha:** `_ControllerActor.get_deployment` only returns `HOT` replicas, so a
> model with only `WARM` replicas looks undeployed to the queue. It asks for a
> new replica, placement promotes the `WARM` one, and the user still watches a
> `PROVISIONING` → `DEPLOYING` cycle for a model that never left the node.

## Sizing a model

`ModelEvaluator` (`src/ndif/services/ray/deployments/controller/cluster/evaluator.py`)
asks nnsight for the checkpoint's weight bytes — from its published metadata
where it can, without loading weights — and pads:

```
padded = ceil(base + base * padding_factor + padding_bias)
```

The two terms cover two shapes of overhead. The **bias**
(`NDIF_DEFAULT_PADDING_BIAS`, 500 MiB) covers the roughly constant per-actor
cost — CUDA context, workspace, allocator floor — which a percentage
underestimates for a small model. The **factor** (`NDIF_DEFAULT_PADDING_FACTOR`,
0.15) covers activation and KV-cache memory that grows with the model, which a
constant underestimates for a large one. There is no per-request memory
reservation beyond this padding. Both are overridable per deploy, and
`DeploymentConfig.size_bytes` replaces the estimate outright.

The estimate depends on `dtype` and on the key's `trust_remote_code`, so the
controller pins each config's dtype to a concrete value before evaluating: the
number that places the replica must be the number the actor loads with. Sizing in practice — which knob to turn, and what
the ledger cannot see — is in
[Model OOM on deploy](../runbooks/model-oom-on-deploy.md).

## Placement

For each model in a deploy call, biggest first, every node is graded and the
best grade wins; ties are broken at random:

| `CandidateLevel` | Meaning |
|---|---|
| `CACHED_AND_FREE` | node has a WARM copy and enough free GPU memory |
| `FREE` | enough free GPU memory |
| `CACHED_AND_FULL` | WARM copy present, but evictions are required |
| `FULL` | evictions are required |
| `CANT_ACCOMMODATE` | not placeable on this node |

A model needs `ceil(size / per_gpu_memory)` cards (or `DeploymentConfig.gpus`),
rounded up to a count it shards into evenly when tensor parallelism is on. Each
card is charged its **share**, `ceil(size / count)`, not the whole card, so
several models can share a GPU. Among cards that fit, the least-available sorts
first — best-fit packing, so small models fill partly used GPUs and leave whole
ones free for large models. A node that refuses for a nameable reason (a GPU
count the model cannot split into) says so in the deploy error; otherwise the
error is `CANT_ACCOMMODATE`.

`DeploymentConfig.replicas` is **additive**: deploying a model that already has
two replicas with `replicas=1` gives you three. Shrinking is `evict`'s job.

### `trust_remote_code` is in the key

Whether a model runs its own repo code is a field of its model key, so every
replica of a deployment loads the same way; see
[Models and Deployment](../operating/models-and-deployment.md#trust_remote_code-in-the-model-key).
Who may deploy such a key is in
[Auth and Limits](auth-and-limits.md#trust_remote_code).

## Eviction

Eviction happens for one reason: to make room. When free memory isn't enough,
the node picks, per GPU, the smallest evictable occupants until the shortfall is
covered, preferring GPUs whose plans need the fewest evictions. Two rules decide
what is evictable:

- **Pinned deployments are never evicted** to make room.
- **The minimum-deployment-time rule:** a deployment younger than
  `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default `3600`) cannot be evicted *to
  place a non-pinned model*. Deploying something pinned ignores the age rule (but
  still can't touch pinned neighbours). The rule stops two models that each need
  most of a GPU from evicting each other on alternating requests.

The consequence to remember: on a full cluster, a request for a new model can
come back `CANT_ACCOMMODATE` purely because the current occupant was deployed 40
minutes ago. Nothing retries it — the request errors and the user tries again
later. An explicit `ndif evict` bypasses both rules.

An evicted `HOT` replica does not simply die. It releases its GPU reservation
and, if its actor class is cacheable and the node's CPU cache can make room
(dropping the smallest `WARM` entries if needed), is demoted to `WARM` keeping
its `replica_id`; otherwise it is removed and the actor killed. The CPU cache
budget is the node's RAM scaled by `NDIF_MODEL_CACHE_PERCENTAGE` (default `0.9`).

> **Gotcha:** `NDIF_MODEL_CACHE_PERCENTAGE` budgets **CPU RAM** for the WARM
> cache, not GPU memory. It is the wrong lever for a GPU OOM.

**Both outcomes are transparent to an in-flight request.** A removed actor makes
the queue's Ray call raise `ActorDiedError`; a demotion cancels the running
request in `to_cache()` and raises `CachedActorError`. Both are in
`EVICTED_ERRORS` (`src/ndif/services/api/queue/replica.py`), so the queue drops
the replica, puts the request back at the front of its priority group, and
re-provisions if work remains. The user sees no error.

The policy code (`Node.evictable`, `Node.find_evictions`, `Node.evict`) is walked
through in
[Controller internals](../developing/controller-internals.md#choosing-and-executing-evictions).

## From decision to actors

The controller never mutates Ray directly from a decision. A deploy or evict
changes its in-memory model of the cluster; the controller then diffs that model
against what it last applied and turns the delta into actor operations — kill,
demote, promote, create — in an order that frees memory before spending it
([build and apply](../developing/controller-internals.md#build-and-apply)). A
separate loop re-reads the Ray node list every `NDIF_CONTROLLER_SYNC_INTERVAL_S`
(default 30), so joining nodes are picked up and a departed node's deployments
are purged.

The queue is only loosely coupled to this. An out-of-band deploy or evict (CLI,
dashboard) pushes a `reconcile_model` event so the affected `Processor` adopts
any replica added; replicas the controller dropped leave the pool through the
`EVICTED_ERRORS` path above.

## Pinning and boot-time deployments

`NDIF_DEPLOYMENTS` is a `|`-separated list of model keys the controller deploys
**pinned** at startup. Pinning is also available per deploy call
(`DeploymentConfig(pinned=True)`, what `ndif deploy --pinned` sends). A pinned
deployment is exempt from eviction and is reported without the
`schedule.end_time` that non-pinned deployments carry in `status()`.

Replicas the *queue* provisions are never pinned — `Replica.provision` asks
`controller.scale` with a bare `DeploymentConfig()`, and `scale` copies a live
replica's class, dtype and GPU count but not `pinned` — so autoscaled capacity
is always reclaimable.

## Related

- [Controller internals](../developing/controller-internals.md) — the reconcile
  loop, `Cluster`/`Node`/`Deployment`/`ModelEvaluator`, eviction and build/apply
  in detail.
- [Models and deployment](../operating/models-and-deployment.md) — actually
  deploying, pinning, revisions, dtype, PEFT.
- [Model actor](../developing/model-actor.md) — what happens inside the actor on
  load, `to_cache`, and `from_cache`.
- [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) — sizing in
  practice: the padding knobs, what the ledger can't see, and making room.
