---
title: "Proposal: untrusted code on a tensor-parallel model"
one_liner: One runner process holding one set of workers, talking to every rank over the sandbox protocol unchanged. Why the ranks need no new decision channel, what the runner has to do that a single host doesn't, and the two problems that need real design.
tags: [internals, dev, proposal, sandbox, tp]
related: [src/ndif/services/ray/tp/ARCHITECTURE.md, src/ndif/services/ray/sandbox/ARCHITECTURE.md, docs/developing/sandbox-internals.md, docs/concepts/sandbox-execution.md]
sources: [src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/nns.py, src/ndif/services/ray/tp/shard.py, src/ndif/services/ray/tp/host.py, src/nnsight/intervention/interleaver.py]
---

# Proposal: untrusted code on a tensor-parallel model

**Status: not implemented.** A tensor-parallel placement currently replaces the
actor class, so an untrusted request on a sharded model runs in-process next to
the weights. This is how to fix that.

## The shape

One runner process, holding one set of workers, with a socket to **every rank**.
Each rank behaves as an ordinary sandbox host. The runner is the only party that
knows there is more than one.

```
        ┌──────── runner (untrusted user code) ────────┐
        │   one set of greenlet workers                │
        └───┬──────────┬──────────┬──────────┬─────────┘
            │          │          │          │   the sandbox protocol, unchanged
         rank 0     rank 1     rank 2     rank 3
         (actor)    (shard)    (shard)    (shard)
            └──────── NCCL, for the model itself ───────┘
```

## Why this needs no new decision channel

The hard part of putting untrusted code on a sharded model is that **whether to
gather a value depends on where the user's workers are parked**, and every rank
has to reach that decision identically or NCCL deadlocks. Today the ranks agree
because each runs the block.

They can also agree without running it. `Interleaver.observed` reads only
`mediator.pending`, `mediator.iterations` and `mediator.caches`
(`nnsight/intervention/interleaver.py`), and under the sandbox those mediators are
`MediatorProxy` objects living in the **host** process
(`sandbox/model.py:77-173`) — mirrors of the runner's workers, updated by `adopt`
the moment a park arrives. So if the runner sends the same parks to every rank,
every rank holds the same mirror and answers `observed()` **locally**.

That is the whole reason this design is cheap:

- **No message per location.** ~196 sharded location-sides per forward for
  Llama-3.2-3B, and none of them cost anything.
- **Traffic is proportional to reads**, not to locations. A block that reads five
  activations costs five exchanges, whatever the model's depth.
- **Nothing crosses a GPU-visible path.** Measured: one CPU-visible read of a
  device value costs 10 µs on an idle stream but **394 µs mid-forward**, because
  it stops the CPU running ahead to enqueue kernels. Any design that puts the
  gather decision on NCCL pays that per decision. This one has no decisions to
  send.

## Life of a request

**1. Dispatch.** Rank 0 receives the request and acquires a runner from its pool.
It hands the runner a connection to each rank and sends the payload once
(`payload, compress, dtype, seed`).

**2. Build.** The runner deserializes the block and creates its workers. A
failure here is reported before any rank has started a forward — the existing
safe-failure point, and the reason the current TP protocol is two-phase at all.

**3. Start.** The runner sends `INTERLEAVE(fn_name, parks, batch_groups, invokes)`
to **every** rank.

**4. Mirror.** Each rank builds its own `MediatorProxy` per worker from those
parks. All ranks now hold identical worker state.

**5. Forward.** Every rank runs it. NCCL between them exactly as today.

**6. At each location, each rank asks itself whether anyone is parked here.**
- *No* — nothing happens. No socket, no message. The overwhelmingly common path.
- *Yes* — the rank gathers if the location is a fragment (NCCL, as today), then
  sends `RESUME(mediator_id, value)` and waits.

**7. Barrier.** The runner waits until **all** ranks have sent `RESUME` for that
serve, switches the worker **once**, and sends the resulting park back to all of
them. The worker runs a single time.

**8. Writeback.** The runner sends the worker's value back to every rank; each
splices it in and re-splits its own shard locally (`fragment()` is a local op, no
collective). **This is where the design pays off**: an edit reaches every rank
over sockets the runner already holds, with no broadcast machinery.

**9. Repeat** 6–8 per read.

**10. Finish.** Each rank reports `DONE`. The runner collects the saved values and
sends `END` with the blob. Rank 0 uploads and answers the client; the other ranks
discard theirs, as they do today.

## What is genuinely new

Everything else is code that exists.

| | |
|---|---|
| The runner holds N connections instead of 1 | plumbing |
| **The runner barriers N ranks per serve and switches the worker once** | the real new logic |
| Shards accept a runner connection | they only talk to rank 0 today |
| Rank 0 still owns the request lifecycle | status, timeout, upload, abort — unchanged |

## The two problems that need real design

### `.source` writeback would corrupt the shards

The runner writes back **every** read as a swap, unconditionally, and says so:
*"We always write back (any object may have been edited, and we can't tell from
here)"* (`sandbox/nns.py`).

That is harmless for a module boundary, because under tensor parallelism those
values are already identical on every rank — a rowwise layer all-reduces its
output, so `mlp.output` and `attn.output` agree everywhere. It is **not** harmless
for a `.source` value, which is each rank's own shard and genuinely differs. The
runner would take one rank's and write it to all of them, silently overwriting
the others.

**Proposal.** Each rank tags its `RESUME` with `rank_local: bool` — true when the
location is a `.source` op rather than a module boundary. The runner skips the
writeback for rank-local values and warns once that `.source` writes do not apply
on a sandboxed tensor-parallel model. This is consistent with what nnsight
already tells users about `.source` under TP: the value is this rank's shard and
cannot be made whole.

Worth recording: `.source` writes are *already* wrong today for position-dependent
edits. At tp=4 with `gate_proj` at `[1, 11, 2048]` per rank, `x[..., :1000] = 0`
zeroes global columns 0–999, 2048–3047, 4096–5095 — not the first 1000. So this
removes a feature that is only correct for uniform edits in the first place.

### Ordering between the barrier and the collectives

The sequence at a location must be the same on every rank:

```
[gather, if fragmented]  →  [barrier, if anyone parked]  →  [local re-split]
```

Two ranks must never be on opposite sides of that — one inside an all-gather
waiting for a peer that is blocked on the runner. It holds because both the
gather condition (`observed() and fragmented()`) and the barrier condition
(`observed()`) are computed from the mirrored proxy state, so all ranks take the
same branch. **This invariant is the design**, and it deserves an explicit test
rather than a comment.

Note this is *not* the same worry as "the runner might answer ranks
inconsistently". That would require a bug in our own barrier code, or hostile code
in a process that already has no isolation and can hang a request anyway. It is
ordinary implementation risk, not a structural hazard.

## Failure modes

| | what happens |
|---|---|
| Runner dies | every rank's socket closes at once — they all see the same event and unwind together. Symmetric, which is better than the alternatives. |
| One shard dies | rank 0 blocks in NCCL until the execution timeout; the cooperative abort unwinds the rest. Today's behaviour. |
| Block raises | the runner tells every rank; all unwind. Today's `raised`-on-every-rank path. |
| Block never returns | execution timeout → abort flag → ranks stop at the shared checkpoint → rank 0 stops the runner. |
| Barrier never completes (a rank never arrives) | the runner is waiting, the ranks are waiting. Caught by the execution timeout, same as any hang. Needs a bounded wait so it fails as a diagnosable error rather than a stall. |

## What has to be fixed regardless

Two determinism bugs that already exist and that any sandboxed design makes
reachable:

- **`PYTHONHASHSEED` is pinned nowhere.** Neither `tp/common.py:rank_env` nor
  `sandbox/host.py:spawn` sets it, so every process gets a randomized hash seed.
  A block whose control flow depends on `set` iteration order can already diverge.
  Under this design only one process runs the block, so it stops mattering for the
  block — but it still matters for anything the ranks compute independently.
- **`seed_ranks` seeds the wrong process.** It calls `torch.manual_seed` in the
  rank process (`tp/common.py`, `tp/model.py`, `tp/shard.py`), but the block's own
  RNG will live in the runner. The seed must ride in the payload and be applied
  there, next to `dtype`.

## Build order

**Phase 1 — extract the driver.** Pull the host side out of
`SandboxModelDeployment` into a `SandboxDriver` that needs only a model, a dtype,
a connection and a responder: `next_event`, `interleave`, `_build_proxies`,
`_assemble`, `install_source`, `run_module`, `check_dangling`
(`sandbox/model.py:239-420`). Prove it against the existing untrusted suite on the
single-GPU path, with no tensor parallelism involved.

This phase is worth doing on its own merits and **is common to every candidate
design** — a rank cannot act as a sandbox host until the host logic is separable
from the Ray actor. Doing it first means the choice of topology stays open.

**Phase 2 — determinism and seeding.** The two fixes above, on today's in-process
TP path, before a runner is involved.

**Phase 3 — the barrier.** Teach the runner N connections and the
wait-for-all/switch-once/reply-to-all cycle. Testable with fake ranks and no GPUs
at all, which is where the ordering invariant should be pinned.

**Phase 4 — shard as host.** `shard.py` accepts a runner connection and drives a
`SandboxDriver` instead of running the block itself.

**Phase 5 — combine** into a sandboxed tensor-parallel actor class, and run the
existing conformance suite (`tests/test_sandbox_conformance.py`) against a
tensor-parallel replica: the same script, trusted and untrusted, must return the
same bytes.

## What to measure before committing

- **Value bytes per read at tp=8.** Every rank ships its value to the runner. For
  a small activation that is nothing; for `tracer.cache()` over many modules it is
  N× the traffic. Likely optimization: only rank 0 ships the value and the others
  send a marker, since for every non-`.source` location they are identical anyway.
- **Barrier latency under real load** — measured 12 µs for a 1-byte fan-out to 3
  peers and 43 µs for 45 KB, but on an idle box with no GPU work in flight.
- **Runner acquire skew.** Every rank needs its connection before the forward
  starts; a cold spawn is ~4 s. This decides pool sizing.
- **Footprint.** One runner (~420 MB host) plus its CUDA context (~504 MB) on
  whichever card it lands on — against N of each for a runner-per-rank design.
  The GPU half sits outside `set_process_limits`, so the controller's padding
  needs to know about it.
