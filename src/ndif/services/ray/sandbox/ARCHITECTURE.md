# The process sandbox

Runs a request's **user intervention code** in a separate process from the
**model**, driving the two in lockstep over a Unix socket. The model stays on the
host GPU (weights never move); only the traced block — the arbitrary Python a user
wrote inside `with model.trace(...)` — is shipped to a runner process and executed
there.

> **Isolation caveat.** Today the runner is just a separate OS process with *no*
> hardening (no namespaces, seccomp, rlimits, or filesystem jail). What it gives
> you is a *seam*: user code executes somewhere other than the model actor, with a
> narrow socket protocol between them, and each request gets a fresh process so
> nothing leaks between them. Hardening can be added behind that seam later
> without touching the interleaving logic.

---

## Why

In a normal NDIF model actor ([`deployments/modeling/base.py`](../deployments/modeling/base.py)),
the deserialized traced block runs **in the actor process**, right next to the
loaded weights. That block is untrusted user Python. The sandbox moves that
execution out of the actor process while keeping the model where it is.

The hard part is that the block and the forward pass are **interleaved**: nnsight
lets user code read and edit a model's intermediate activations as the model runs
(`hidden = model.layer.output`, `model.layer.output = ...`). So the two can't run
independently — they take strict turns. The sandbox reproduces that turn-taking
across a process boundary.

---

## The pieces

| File | Role |
|------|------|
| [`model.py`](model.py) | `SandboxModelDeployment` — the actor. Loads the model on the host (base class), owns a pool of runners, drives one per request, and holds the **host side** of the interleaver (`MediatorProxy`). |
| [`host.py`](host.py) | Host-side plumbing: `spawn` a runner, `Pool` that pre-warms runners and hands out a **fresh one per request** (stopped when done), `Sandbox` handle, and the transport `Connection`. |
| [`runner.py`](runner.py) | The runner process (`python -m sandbox.runner <socket> <model_key>`). Accepts a connection, receives the payload, and calls `nns.run`. Importing `nns` here installs the IPC patches — **in the runner, never on the host**. |
| [`nns.py`](nns.py) | The nnsight glue that runs **inside the runner**: deserialize the tracer, execute the block, and host the **worker side** of the interleaver (`IPCInterleaver`, patched `Mediator.event`, `IPCEnvoy`). |
| [`protocol.py`](protocol.py) | The wire contract: framing, a type-tagged codec, and the authoritative **message catalog**. |

```
        ┌──────────────── model actor process (host) ─────────────────┐
        │  SandboxModelDeployment                                      │
        │    • model + weights on GPU                                  │
        │    • self.model.interleaver  ← real PyTorch hooks            │
        │    • MediatorProxy per worker  (parent side)               │
        └───────────────▲───────────────────────┬─────────────────────┘
                        │  Unix socket (protocol.py)
                        │  RESUME / PARK / THROW / DONE / INTERLEAVE / …
        ┌───────────────┴───────────────────────▼─────────────────────┐
        │  runner process (meta model, no weights)                     │
        │    • nns.run: deserialize + execute the traced block         │
        │    • IPCInterleaver + worker greenlets  (worker side)        │
        └──────────────────────────────────────────────────────────────┘
```

---

## A request, end to end

1. **Dispatch.** `run()` is inherited from `BaseModelDeployment` — a **template**
   that responds `RUNNING`, drives `execute()` on a worker thread **raced against
   the execution timeout and a cancel signal** (`kill_switch`), emits
   metrics/event logs, and does `cleanup`/`restart` plus the `COMPLETED`/`ERROR`
   responses. The sandbox overrides only the hooks that differ:
   - `execute(request)` — the worker-thread body: if `request.trusted` it defers to
     the base (in-process, no sandbox); otherwise it drives a fresh runner (below).
   - `interrupt` (also `stop()` the runner), `format_error` (`RunnerError` → its
     pre-formatted text), `cleanup` (discard the runner too), `execution_scope`
     (nothing — PRINT events, not host stdout).

   On timeout/cancel the template calls `interrupt()`, which stops the runner —
   closing the socket unblocks the host thread parked on `recv`.

2. **Ship the payload.** `run_in_process` acquires a warm runner from the `Pool`,
   opens a `Connection`, and sends `(request.payload, request.compress)` — the
   serialized tracer blob. That's the whole handoff; there's no bootstrap closure.

3. **Runner executes.** The runner's `handle` receives `(blob, compress)` and calls
   `nns.run`, which:
   - Deserializes the tracer with `IPCCloudUnpickler`. Persistent ids resolve
     specially here: the **interleaver** → an `IPCInterleaver` bound to this socket;
     the **model, its modules and its tokenizer** → the runner's own *meta* build
     (structure, no weights), registered by `load_meta_model` at startup; anything
     unknown → `None`. The weights live on the host and are never touched here, so
     every activation still crosses the socket.
   - Runs `tracer.execute(...)`. The block runs as a set of greenlet **workers**.
     Whenever the block calls the model, it hits `IPCEnvoy.interleave`.

4. **Interleave.** `IPCEnvoy.interleave` starts the workers (each parks on its first
   activation request), ships one **initial park per worker** in an `INTERLEAVE`
   message, and then **pumps** the socket. On the host, `interleave` builds one
   `MediatorProxy` per park, installs them as the model interleaver's mediators,
   and runs the real forward pass. As the model reaches each location, the proxies
   drive their workers over the socket (details below).

5. **Result + saved values.** When the forward pass returns, the host ships the
   result back (`DONE`) and the runner's `interleave` returns it to `tracer.execute`.
   `nns.run` then collects the block's `nnsight.save()`-marked locals, `torch.save`s
   them (CPU-relocated), and sends the **bytes** in `END`.

6. **Upload.** The host uploads those bytes as-is via `upload_bytes` (object store),
   and responds `COMPLETED` with a presigned URL the client downloads — identical to
   the non-sandbox path, minus a pickle round-trip.

---

## Interleaving: the split interleaver

This is the core. In stock nnsight ([`nnsight/.../interleaver.py`](../../../../../nnsight/src/nnsight/intervention/interleaver.py)),
one `Interleaver` owns PyTorch hooks and a list of `Mediator`s. Each mediator runs
one block of intervention code in a **greenlet** (the "worker"). The worker runs
until it needs a value, then **parks** on a location; the model runs until it
reaches that location, hands the value over, and the worker resumes — possibly
editing the value on the way back. Worker and model take strict turns on one
thread, coordinated by greenlet switches.

The sandbox splits each `Mediator` **across the socket**:

- The **worker greenlet stays in the runner.** Only the model is remote, so the
  socket boundary sits at the model↔mediator seam.
- The **parent side moves to the host** as `MediatorProxy` — one per worker. The
  proxy owns the occurrence counter, the `tracer.iter` pin, and the read/swap
  matching, and drives its worker over the socket instead of by greenlet switch.

Crucially, **`MediatorProxy` subclasses `Mediator` and reuses its `handle`
unchanged.** All the intricate matching/iteration/relaxation logic is the real
nnsight code. The proxy only overrides the two things that touch the boundary:

- `adopt` — re-tags each *untagged* park the worker sends into the
  `(event, "{location}.i{n}", *rest)` shape `handle` expects.
- `switch` — turns a greenlet hop into a `RESUME`→`PARK` socket round-trip.

### The exchange

```
host: forward pass reaches location L, value V
        Interleaver.handle(L, V)  →  for each proxy: proxy.handle(L, V)   [real Mediator.handle]
            if this worker is parked on L at this occurrence:
                proxy.switch(V)  ──RESUME(id, V, pin)──▶  runner: worker.switch(V)
                                                              worker reads V, maybe edits, parks again
                     proxy.adopt(park) ◀──PARK(id, park)──  runner sends the worker's next park
            (a swap's replacement rode back inside the park; the proxy substitutes it into V)
        returns the possibly-edited V into the forward pass
```

- A **read** (`model.layer.output`) crosses only when the worker is resumed: the
  value rides in `RESUME`. Locations no worker is parked on never cross.
- A **swap** (`model.layer.output = x`) rides back in the worker's `PARK` (its
  pending event carries the replacement); the proxy substitutes it.
- `PARK(id, None)` means the worker finished.
- After the run, `check_dangling` finds proxies still parked (workers that wanted a
  location the model never reached) and sends `THROW`; the runner throws
  `OutOfOrderError` into that worker — or warns, for an open-ended `tracer.iter` that
  outran the model.
- `tracer.stop()` in a worker raises `EarlyStopException`; the runner sends `STOP`,
  the proxy re-raises it into the forward pass, and the model's interleaver swallows
  it as an intentional early stop.

Why one proxy **per** worker (not a single forwarder)? Two reasons:
1. **Per-mediator iteration.** Each worker tracks occurrences independently; the
   host must too, so the `.i{n}` tag is preserved, not collapsed.
2. **Batch slicing.** Each worker owns a slice of a batched forward pass. The
   runner ships each worker's `batch_group` (its `[start, size]` rows) in the
   `INTERLEAVE` message; the host sets it on the proxy and registers the batcher on
   the interleaver, so nnsight's own `narrow`/`widen` pull *that worker's* rows out
   of an activation on a read and stitch an edit back on a swap — inherently
   per-worker.

### Iteration authority lives on the host

A location can be reached many times in one run (a generation loop revisits every
module per token). nnsight tags each visit with an occurrence index and lets
`tracer.iter` pin a worker to specific steps. That machinery is split between two
methods — `Mediator.handle` *counts* visits and *relaxes* the pin, while
`Mediator.event` *tags* a park from that count. To avoid a shadow counter, the
sandbox moves **all** of it to the host:

- Patched `Mediator.event` (runner) parks **untagged**: it sends the raw location
  plus the worker's current pin, not a resolved `.i{n}` tag.
- The proxy (host) owns the counter, computes the tag in `adopt`, matches in
  `handle`, and on each `RESUME` **pushes the pin back** so `tracer.iter`
  relaxation stays in lockstep with the worker.

---

## Saved values

The non-sandbox path collects the block's `nnsight.save()`-marked locals and
`torch.save`s them for upload. In the sandbox, the block runs in the runner, so:

- The **runner** collects the marked locals (same `_saves()` / frame-locals filter
  as `BaseModelDeployment.execute`) and `torch.save`s them with the shared
  CPU-relocating `cpu_pickle_module` (imported from `deployments/modeling/util.py`),
  then ships the **bytes** in `END`.
- The **host** uploads those bytes directly via `upload_bytes` — no unpickle/repickle
  round-trip.

---

## Why `nns` is imported only in the runner

Importing `nns` runs its module-level patches — it rebinds `Mediator.event` and
grafts `IPCEnvoy`'s overrides onto the base `Envoy` **process-wide**. On the host
that would break the real model (its `interleave` would start forwarding to a
socket instead of running the forward pass). So the import lives in `runner.py`,
executed in the runner process; the host never imports `nns`. This is also why the
handoff is a plain `(blob, compress)` message rather than a pickled callable — the
runner already knows to run `nns.run`.

The runner runs as `python -m ndif.services.ray.sandbox.runner <socket> <model_key>`
(with the repo root on `PYTHONPATH`), i.e. as a normal `ndif` submodule, so it can reuse ndif
helpers rather than duplicate them: `nns.run` deserializes via nnsight's own
`RequestModel.deserialize(..., unpickler=IPCCloudUnpickler)` (an unpickler hook
added to nnsight), and imports `cpu_pickle_module` from `deployments/modeling/util`.
The `__init__` chain down to the runner is empty, so this stays light.

The whole-class Envoy patch (rather than a single-method monkeypatch) is needed
because the tracer's root envoy is a model-wrapper *subclass*
(`TransformersModel → … → Envoy`) that inherits `interleave`; rebinding the module's
`Envoy` name wouldn't reach it, so the overrides are grafted onto the base class
every envoy shares.

---

## Current simplifications

These are deliberately deferred (correctness-first, optimize later):

- **No isolation** — the runner is an ordinary process (see the top caveat).
- **Device placement is host-side only** — assembled inputs and ad-hoc `CALL` args
  are moved onto the model's device on the host; activations otherwise cross as-is,
  which assumes the runner shares the host's GPU (true on a single box).
- **No autocast** — the base path wraps execution in `torch.autocast(model dtype)`;
  the runner doesn't know the model dtype yet.
- **exec_ms spans the whole run** — `ExecutionTimeMetric` / `GPUMemMetric` /
  `RequestResponseSizeMetric`, the structured `event()` logs, and the
  `deserialize_ms` / `upload_ms` split are all emitted. After the template
  refactor `exec_ms` covers the whole worker-thread body (deserialize + execute)
  for **both** base and sandbox — so it overlaps `deserialize_ms` rather than
  excluding it as the base did before.

---

## Wire protocol

The authoritative message catalog (both directions, payload shapes, and the
runner→host `pack` vs host→runner single-`encode` asymmetry) lives in the
[`protocol.py`](protocol.py) module docstring. In brief:

- **host → runner:** `(blob, compress)`, `RESUME`, `THROW`, `DONE`
- **runner → host:** `INTERLEAVE`, `PARK`, `STOP`, `PRINT`, `END`, `EXCEPTION`
