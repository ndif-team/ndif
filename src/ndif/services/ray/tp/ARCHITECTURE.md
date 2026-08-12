# Tensor-parallel model serving

> **Opt-in.** None of this runs unless `NDIF_TP_MODEL_ACTOR_CLASS` is set to
> `ndif.services.ray.tp.model.TPModelActor`. Unset, the controller works out no
> sharding degree, rounds no GPU count up, and places every multi-GPU model with
> accelerate instead.

Serves a model whose **weights are split across GPUs within each layer**, rather
than spread layer-by-layer over them. The GPUs work on the same layer at once,
which needs one process per GPU — and the Ray actor **is rank 0** of that group,
not a coordinator above it.

> The gather that makes a sharded activation readable is **not here**. It lives in
> nnsight (`nnsight.modeling.tp`), because it is a property of tracing a sharded
> model, not of serving one. This directory is processes, sockets and lifecycle.

---

## Why not `device_map`

The base actor spreads a model with accelerate: whole layers on each card, run one
after another. That works and needs no coordination, but only one GPU computes at
a time.

transformers native tensor parallelism (`distributed_config=DistributedConfig(tp_size=N)`)
splits each layer's weights instead, so every GPU works on every layer. The cost
is that it is **SPMD**: N processes running the same program in lockstep, joined
by NCCL collectives. Everything awkward below follows from that one fact.

---

## The pieces

| File | Role |
|------|------|
| [`model.py`](model.py) | `TPModelDeployment` — the actor, and rank 0. Inherits `run()` (statuses, timeouts, metrics, upload) from the base actor untouched; overrides only loading, committing, aborting and settling. |
| [`host.py`](host.py) | `ShardGroup` — rank 0's side of the other ranks: spawn them, hand each request over, collect what they report, kill them. Deliberately free of Ray and of the request types, so the whole process-management path can be exercised on its own. |
| [`shard.py`](shard.py) | A non-zero rank (`python -m ndif.services.ray.tp.shard`). Loads its shard, then runs whatever rank 0 sends. Talks to no one else: no responses, no uploads, no metrics. |
| [`common.py`](common.py) | What both sides need: the framed `Channel`, the rank environment, seeding, sharded loading, and the cooperative `AbortController`. |

---

## The three things that are genuinely different

Everything else about a request is the base actor's.

### Every rank runs the user's block

Not just rank 0. Whether a sharded activation gets gathered depends on where the
user's interventions are parked, so **all ranks must reach that decision
identically** or a collective fires on some ranks and not others — which deadlocks
NCCL, or silently reduces mismatched tensors. So the shards execute the same
Python against the same seed, and rank 0 alone answers the client and uploads.
The values a shard computes are identical to rank 0's (they come from gathered
tensors) and are dropped.

This is also why a block whose **control flow** diverges across ranks hangs, and
why nothing may branch on rank. The one rank-dependent value nnsight will hand a
user is a `.source` read, which warns for exactly this reason.

### Cancelling is cooperative

The base actor kills the execution thread. Doing that here would strand every
other rank in a collective with no way back, so instead a flag is set and the
ranks stop together at a shared checkpoint — a pre-hook on the root module, so it
fires *between* forwards and never inside a gather. A group that has not settled
within `SETTLE_TIMEOUT_SECONDS` is restarted, which is expensive and always
terminal.

### The GPUs are renamed

transformers picks a rank's device by `LOCAL_RANK` used as a CUDA index, so the
whole group runs under a `CUDA_VISIBLE_DEVICES` listing only the assigned cards.
**Inside these processes a device index is a position in that list, not a physical
GPU id.** Rank 0's own bookkeeping is rewritten to device 0 accordingly.

---

## Bringing the group up is a two-sided rendezvous

Getting this backwards deadlocks silently, and it is not obvious from either side.

Loading is where transformers forms the process group, and it blocks until *every*
rank arrives — rank 0 included. So a shard cannot report "loaded" before rank 0
loads, and rank 0 must not wait for the shards before loading itself. `ShardGroup`
splits the handshake:

```
HELLO       shard has connected           (before it loads anything)
  ...rank 0 loads its own shard, which is what releases them...
READY       shard's weights are loaded    (collected by wait_ready)
```

The symptom of waiting in the wrong order is every process sitting at ~414 MiB — a
CUDA context and no weights — forever.

---

## A request, in two phases

```
PREPARE  ->  every shard deserializes the block and answers READY or ERROR
GO       ->  every rank runs it, and the shards answer DONE or ERROR
```

The split exists because **deserialization is where a request most often dies**
(version drift between the client's block and the server's tree) and it is the
last point before any collective. So the shards prove they can build the block
before rank 0 commits to a forward. Past `GO`, a shard that drops out leaves rank
0 blocked in NCCL until the execution timeout.

An untrusted request uses the same two phases with `SANDBOX` in place of
`PREPARE`. Nothing is built on a shard: one runner process holds the block for the
whole group, and each rank is a *host* to it — driving the forward and answering
reads, exactly as the single-GPU sandbox actor does. `READY` then means "the
environment is applied and I can reach the runner", which is still the last point
before any collective.

The runner serves each worker once for the whole group rather than once per rank
(`sandbox.protocol.Fanout`), so the block runs a single time. Rank 0 must connect
to the runner *before* handing the path out, because the runner takes its first
connection as the one that sends the payload.

A request that never reaches `GO` is stood down with `SKIP`, and each shard
answers `IDLE`. That acknowledgement is load-bearing: "the group is back at rest"
has to be something rank 0 **observes**, not assumes, or a stood-down shard is
indistinguishable from a wedged one — which is how every failed deserialization
used to restart the replica.

---

## Reading the outcome

`collect` returns two lists and the caller must not merge them:

- **raised** — the shard reported an exception and went back to idle. Alive and
  reusable. This is the *normal* outcome when the user's block raises, because
  every rank runs that block.
- **lost** — the shard never answered. Not a state another request can start from.

A shard that raised **while rank 0 did not** is the dangerous case: the ranks have
diverged, and there is no way back. That, and `lost`, are what cost the replica.
Reading any exception as fatal tore down four GPUs every time someone had a bug in
their code, and the person who paid was whoever sent the next request.

---

## Current simplifications

- **No HOT↔WARM caching.** A group cannot be parked: every rank's device is fixed
  when its process starts, and restoring may land on different cards. The actor
  sets `CACHEABLE = False` and the controller evicts these outright.
- **The sandbox is wired but not selectable.** A shard can now serve an
  untrusted request as a *host* to one runner shared by the whole group
  (`SANDBOX` below), but nothing chooses that path yet: the controller returns
  one actor class, so "sharded" and "sandboxed" are still mutually exclusive by
  construction. See `docs/developing/sandboxed-tensor-parallel-proposal.md`.
- **Dense models only.** Expert-parallel styles that slice by expert rather than
  along the last dim are refused at load; nnsight's `SHARDED_SIDES` is the list.
- **Rank 0's metrics only.** Its peak memory is representative under TP.
- **transformers >= 5.15.** Below it a tied LM head is not sharded while its
  output is still gathered, so logits come back `tp_size` times too wide with a
  plausible argmax. `requirements.txt` carries the floor.

---

## Where the rest lives

| Question | Go to |
|---|---|
| How is a sharded activation made whole for a trace? | `nnsight.modeling.tp`, and `nnsight.intervention.fragments` for when |
| Who decides a model is placed tensor-parallel? | [`controller/cluster/evaluator.py`](../deployments/controller/cluster/evaluator.py) — `actor_class`, `tp_degree` |
| How many ways does this checkpoint split? | nnsight's `Remotable.max_tp_size`, read from the config |
| What does a request do around all this? | [`deployments/modeling/base.py`](../deployments/modeling/base.py) — `run()` |
