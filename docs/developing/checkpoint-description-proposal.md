---
title: "One call to describe a checkpoint"
one_liner: Why the controller now asks nnsight one question about a checkpoint instead of four, why max_tp_size deliberately isn't one of them, and the two live problems the change fixed.
tags: [internals, dev, controller]
related: [docs/developing/controller-internals.md, docs/operating/models-and-deployment.md, docs/concepts/deployments-and-eviction.md]
sources: [src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/controller.py]
---

# One call to describe a checkpoint

**Status: implemented.** Written down after an audit of the tensor-parallel work,
then built. Kept because the reasoning outlived the change — particularly the
part about what `max_tp_size` is, and one correction the implementation forced.

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

## The two problems it fixed

### It runs on the controller's event loop (and the timeout story is smaller than it looked)

`estimate_bytes` calls `HfApi().model_info`; `max_tp_size` and
`checkpoint_config` each call `AutoConfig.from_pretrained`. None passed a
timeout, and all of it happens inside `Cluster.deploy`, which is synchronous
inside the controller's `async def deploy`.

**Correction, found while fixing it:** "no timeout" was true of the API surface
and misleading about the behavior. `huggingface_hub` bounds everything it fetches
at 10s by default (`HF_HUB_ETAG_TIMEOUT`, `HF_HUB_DOWNLOAD_TIMEOUT`), so the
config read was already bounded per request — `AutoConfig.from_pretrained` takes
no timeout argument, but its transport has one. Only `model_info` needed a
timeout passed explicitly, and now gets one.

A first attempt bounded the config read by running it on an abandoned daemon
thread. Do not do this. A thread left blocked in an uninterruptible wait stops
the **process** exiting — verified, the interpreter hangs — which in a model
actor means one that will not shut down when told. That is a worse failure than
the slow Hub it was guarding against.

What was real, and remains the reason for the change: a slow Hub parked the
**entire controller event loop** for as long as it took. `check_nodes` stopped,
every `_monitor_deployment` froze, every RPC queued behind it — and nothing about
that failure named the Hub. It looked like the controller hanging.

A *fast* failure was worse in a different way. `HuggingFaceModel._config` swallowed
its exception and returned `None`, so `max_tp_size` returned `None`, and a model
that shards perfectly well was silently placed non-tensor-parallel with only a
`logger.debug` to say why. "Couldn't reach the Hub" and "this model doesn't shard"
were the same observable outcome.

**Done:** `model_info` gets an explicit timeout; a failure to *read* now raises
`CheckpointUnreachable` rather than collapsing into `None`; and the evaluator logs
that case at warning, saying in as many words that it is a network result and not
a property of the model. The blocking is addressed separately — `Controller`
warms the evaluator's cache with `asyncio.to_thread` before placing, so the
network happens off the loop while placement itself stays on it, one deploy at a
time, exactly as before. Moving placement to a thread would have let it interleave
with `check_nodes` over the same cluster state, which is a much larger change than
this problem is worth.

### The same config is fetched twice per cold entry

`_remoteable_max_tp_size` and `_remoteable_checkpoint_config` each call
`AutoConfig.from_pretrained` through a shared private reader that does not cache.
Two network round trips for one object, on the path that is already blocking the
event loop.

**Done:** `_config` memoizes on `(model_key, trust_remote_code)`. Measured on a
live stack: a cold `describe_checkpoint` for Llama-3.2-3B takes 1.14s, the
`max_tp_size` that follows it 0.00s, and a repeat description 0.04s.

## The interface

The four pairs became one, as sketched here:

```python
@dataclass
class CheckpointInfo:
    """What a placement decision needs to know about a checkpoint, without
    loading it. Every field optional: a wrapper answers what it can."""
    size_bytes: Optional[int] = None      # weights at the requested dtype
    n_params: Optional[int] = None
    config: Optional[Any] = None
    revision: Optional[str] = None
    # and deliberately no max_tp_size -- see "The one that does not fit"


class Remotable:
    @classmethod
    def describe_checkpoint(cls, model_key: str, dtype: str, **kwargs) -> CheckpointInfo:
        """Public entry: resolve the wrapper class from the key and ask it."""

    @classmethod
    def _remoteable_describe_checkpoint(cls, key: str, dtype: str, **kwargs) -> CheckpointInfo:
        """Hook. Base: size from the parameter count, everything else None."""
```

`HuggingFaceModel` reads the config **once** and answers everything from it plus
one Hub call; `_remoteable_estimate_bytes` delegates to it rather than sizing
independently, so the two cannot disagree about how big a model is — and one of
them decides where it goes. `ModelEvaluator._entry` is one call, and takes the
Hub's real parameter count when there is one instead of dividing the byte
estimate back out.

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

So `max_tp_size` stayed a separate question rather than becoming a field on a
dataclass named "checkpoint info". The honest framing is *"given this runtime, how
can these weights be split"*, and the honest place for it is next to the code that
does the splitting. `CheckpointInfo` has no such field, and a test asserts it
doesn't.

## What still isn't done

`max_tp_size` remains the odd one out, and the seam it sits on is still the one
that produced the `Llama4Config` bug. Nothing here fixes that; both predicates now
read the same table, which makes them agree without making the question live in
the right place. If a second runtime ever shards models differently, revisit it
then — that is the point at which "given this runtime" stops being a hypothetical.
