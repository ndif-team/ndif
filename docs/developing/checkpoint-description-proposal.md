---
title: "Proposal: one call to describe a checkpoint"
one_liner: The controller asks nnsight four separate questions about a model before placing it, fetches the same config twice doing it, and blocks its own event loop on the network. What one call would look like.
tags: [internals, dev, proposal, controller]
related: [docs/developing/controller-internals.md, docs/operating/models-and-deployment.md, docs/concepts/deployments-and-eviction.md]
sources: [src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/controller.py]
---

# Proposal: one call to describe a checkpoint

**Status: not implemented.** Written down after an audit of the tensor-parallel
work. Two of the three problems below are live today; the interface change is the
optional part.

Before the controller can place a model it has to know things about a checkpoint
it has not loaded. Those answers live in nnsight, because the wrapper class is
what knows how to read its own checkpoint. `ModelEvaluator._entry` asks for them
one at a time:

```python
base_size_bytes = Remotable.estimate_bytes(model_key, resolved, trust_remote_code=...)
max_tp          = Remotable.max_tp_size(model_key, trust_remote_code=...)
config          = Remotable.checkpoint_config(model_key, trust_remote_code=...)
revision        = Remotable.checkpoint_revision(model_key)
```

Each is a public/hook pair on `Remotable` — four now, and growing along a
predictable axis (activation footprint, KV-cache size, quantization support,
minimum degree), each addition about 20 lines of boilerplate.

## The two problems that are live now

### There is no timeout on any of it, and it runs on the controller's event loop

`estimate_bytes` calls `HfApi().model_info`; `max_tp_size` and
`checkpoint_config` each call `AutoConfig.from_pretrained`. None passes a timeout.
All of it happens inside `Cluster.deploy`, which is synchronous inside the
controller's `async def deploy`.

A slow or blackholed Hub therefore parks the **entire controller event loop**:
`check_nodes` stops, every `_monitor_deployment` freezes, every RPC queues behind
it. Nothing about that failure names the Hub — it looks like the controller
hanging.

A *fast* failure is worse in a different way. `HuggingFaceModel._config` swallows
its exception and returns `None`, so `max_tp_size` returns `None`, and a model
that shards perfectly well is silently placed non-tensor-parallel with only a
`logger.debug` to say why. "Couldn't reach the Hub" and "this model doesn't shard"
are the same observable outcome.

**Fix, independent of everything else in this document:** put a timeout on the Hub
calls, and make the two cases distinguishable — a network failure should raise (or
be reported) rather than collapse into `None`, which is the answer meaning "this
model genuinely cannot be split".

### The same config is fetched twice per cold entry

`_remoteable_max_tp_size` and `_remoteable_checkpoint_config` each call
`AutoConfig.from_pretrained` through a shared private reader that does not cache.
Two network round trips for one object, on the path that is already blocking the
event loop.

## The interface

Collapse the four pairs into one:

```python
@dataclass
class CheckpointInfo:
    """What a placement decision needs to know about a checkpoint, without
    loading it. Every field optional: a wrapper answers what it can."""
    size_bytes: Optional[int] = None      # weights at the requested dtype
    n_params: Optional[int] = None
    config: Optional[Any] = None
    revision: Optional[str] = None
    max_tp_size: Optional[int] = None     # None: cannot be split at all


class Remotable:
    @classmethod
    def describe_checkpoint(cls, model_key: str, dtype: str, **kwargs) -> CheckpointInfo:
        """Public entry: resolve the wrapper class from the key and ask it."""

    @classmethod
    def _remoteable_describe_checkpoint(cls, key: str, dtype: str, **kwargs) -> CheckpointInfo:
        """Hook. Base: size from the parameter count, everything else None."""
```

`HuggingFaceModel` reads the config **once** and answers everything from it plus
one Hub call. `ModelEvaluator._entry` becomes one call, and `CacheEntry` becomes
roughly the dataclass it already is by hand — which is also where the `revision`
regression came from (a field added to the status and forgotten in the entry,
caught only by a live `/status` returning an error).

## The one that does not fit

`max_tp_size` is not like the others, and its own docstring says so: "splitting a
model is a property of the runtime that loads it, not of the tree alone" — and
then it answers anyway, by asking transformers' `base_model_tp_plan`. NDIF is
really asking *transformers*, through two layers of nnsight indirection plus a
model-key parse.

The evidence that this is the wrong seam is a bug it produced: the predicate that
decides whether a model is *placed* tensor-parallel (`tp/plan.py`) and the one
that decides whether it can be *traced* that way (`tp/interleaver.py`) drifted
apart, because they live in different modules answering the same question and only
one of them has the module tree it will be applied to. `Llama4Config` passed the
first and raised in the second, after the cards were allocated and the weights
read onto them. (Now fixed by making both read the same table — but the shape that
allowed it is still there.)

If `describe_checkpoint` happens, `max_tp_size` should stay a separate question
rather than becoming a field on a dataclass named "checkpoint info". The honest
framing is *"given this runtime, how can these weights be split"*, and the honest
place for it is next to the code that does the splitting.

## Order

1. Hub-call timeouts, and distinguishing an unreachable Hub from an unshardable
   model. Independent, small, and the only part that is a live availability
   problem.
2. Cache the config read so a cold entry fetches once.
3. `describe_checkpoint`, if and when a fifth question shows up. Four pairs is not
   yet a grab-bag; the trigger is the fifth, not the count today.
