---
title: Sandbox Internals
one_liner: How a request's traced block runs in a separate runner process while the model stays on the host, with nnsight's interleaver split across a Unix socket.
tags: [internals, dev, sandbox, ray]
related: [docs/concepts/sandbox-execution.md, docs/developing/model-actor.md, docs/developing/nnsight-integration.md, docs/concepts/request-lifecycle.md, docs/reference/error-messages.md]
sources: [src/ndif/services/ray/sandbox/protocol.py, src/ndif/services/ray/sandbox/host.py, src/ndif/services/ray/sandbox/runner.py, src/ndif/services/ray/sandbox/nns.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/deployments/modeling/base.py]
---

# Sandbox Internals

## What this covers

`src/ndif/services/ray/sandbox/` — the modules that run a request's traced block
(arbitrary user Python) in a separate runner process while the model it
interleaves with stays on the actor's GPUs. **It is process separation, not a
security boundary** — see [What the separation is](#what-the-separation-is).

Two facts frame the design: **the model never moves** (weights load in the actor
and stay there — `src/ndif/services/ray/deployments/modeling/base.py`
`BaseModelDeployment.load_from_disk`), and **the block and the forward pass are
interleaved**, taking strict turns — locally a greenlet switch, here a process
boundary that must not change the semantics.

`src/ndif/services/ray/sandbox/driver.py` is the **host** side — one proxy per
runner worker, the interleaved run, the control events — and knows nothing of Ray
or requests, so a tensor-parallel shard can be a host too
(`src/ndif/services/ray/tp/`). `src/ndif/services/ray/sandbox/model.py` is the
**actor** side: the runner pool, the request lifecycle, and error reporting, most
of it in the `SandboxHost` mixin the sharded actor shares.

This page assumes nnsight's interleaver (`Mediator`, `Event`, park/switch,
occurrence tagging). `src/ndif/services/ray/sandbox/ARCHITECTURE.md` is
authoritative for the *why*, and the `protocol.py` module docstring for the
message payloads; this page adds the request-level context and the lifecycle.

## The first branch: trusted vs untrusted

`BackendRequestModel.trusted` (`src/ndif/common/schema/request.py`; `"trusted"
in request.tags`) decides whether the sandbox runs at all:

| `request.trusted` | Path | Where the block runs |
|---|---|---|
| `True` | `SandboxModelDeployment.execute` defers to `BaseModelDeployment.execute` | in the model actor process, next to the weights — no runner, no socket |
| `False` | `execute` calls `SandboxHost.run_in_runner` | in a separate runner process |

`SandboxHost.execution_scope` forks the same way: trusted reuses the base's
stdout/stderr capture, untrusted gets a `nullcontext` because its output arrives as
`PRINT` messages. How the tags are stamped (the key's user_tags with auth on;
client-supplied tags honored and unspecified ones defaulting to `{"trusted"}`
with auth off) lives in [Auth and limits](../concepts/auth-and-limits.md).

> **Gotcha:** a plain local `just up` has no Postgres, so a request is trusted by
> default and the sandbox path doesn't run. Send `tags: []` in the request to
> force it — no Postgres needed. The design intent is that both paths produce the
> same results for the same request, which is why the sandbox reuses nnsight's own
> `Mediator.handle`, `Batcher` and `Cache` rather than reimplementing them;
> [What the untrusted path cannot do](#what-the-untrusted-path-cannot-do) lists
> where it does not.

## Which actor runs it

The sandbox exists only where `SandboxModelActor` is the deployed class. The
controller's built-in default is the in-process `ModelActor`; the compose stack sets
`NDIF_DEFAULT_MODEL_ACTOR_CLASS=ndif.services.ray.sandbox.model.SandboxModelActor`,
and the tensor-parallel equivalent is
`NDIF_TP_MODEL_ACTOR_CLASS=ndif.services.ray.tp.model.SandboxedTPModelActor`. The
full selection order is in
[Adding a model actor](adding-a-model-actor.md#selecting-your-actor).

## Transport

`src/ndif/services/ray/sandbox/protocol.py` (stdlib + torch only; both processes
import it): length-prefixed frames (`send_frame`/`recv_frame`; `recvn` raises
`ConnectionError` on a half-read, which is what a killed runner looks like), and a
one-byte-tagged codec (`encode`: raw `bytes`, UTF-8 `str`, or `torch.save`).
`torch` because `torch.load` takes a `map_location`, so the receiver places tensors
as they are rebuilt — `SandboxDriver.pump` sets it to the host's model device.
Runner → host uses `pack`/`unpack` (`(values, kwargs)`); host → runner sends one
`encode`d value, so multi-field messages are one tuple.

## Message catalog

A *park* is `(event, location, pin, *rest)`: `event` is `Event.VALUE`/`SWAP`/`SKIP`,
`Event.BARRIER`, or a control-event string; `rest` carries the replacement for
SWAP/SKIP; `pin` is the worker's `tracer.iter` pointer or `None`. Parks cross
**untagged** — the host owns the `.i{n}` occurrence tag. Payload shapes are in the
`protocol.py` docstring; this table maps each message to the code on both ends.

| Message | Dir | Sent by | Handled by |
|---|---|---|---|
| `(blob, compress, dtype, seed, env)` | h→r | `SandboxHost.run_in_runner` | `Runner.handle` → `nns.run` |
| `RESUME` | h→r | `MediatorProxy.switch`, `settle_control`, `release` | `IPCInterleaver.pump` |
| `THROW` (`OUT_OF_ORDER` / `ITER` / `BARRIER`) | h→r | `SandboxDriver.check_dangling` | `IPCInterleaver.throw` |
| `CACHE_HIT` | h→r | `ShippingCache._record` | `pump`, into the runner's `Cache` |
| `DONE` | h→r | `SandboxDriver.interleave` | ends `pump` |
| `INTERLEAVE` | r→h | `IPCEnvoy.interleave` | `SandboxDriver.interleave` |
| `PARK` | r→h | `pump`, `writeback` | `MediatorProxy.adopt` |
| `STOP` | r→h | `pump` on `tracer.stop()` | `MediatorProxy.switch` raises `EarlyStopException` |
| `PRINT` | r→h | `Writer.write` (runner.py) | `SandboxDriver.next_event` → `Status.LOG` |
| `END` | r→h | `nns.run` | `pump` returns the blob |
| `EXCEPTION` | r→h | `nns.run` | `next_event` raises `RunnerError` |
| `SOURCE` / `CALL` / `CACHE` | control park | `ipc_module_source` / `ipc_recursive_source`, `IPCEnvoy.__call__`, `ipc_cache` | `MediatorProxy.settle_control` → `install_source`, `run_module`, a `ShippingCache` |

Control parks name nothing the forward produces: they ride inside a `PARK`, are
drained by `settle_control` before `handle` sees them, and are answered in the next
`RESUME`'s `args`.

## Lifecycle

### Spawn and the pool

`spawn` (`src/ndif/services/ray/sandbox/host.py`) picks `/tmp/sbx-<12 hex>.sock`,
builds the runner's environment from the `RUNNER_ENV` allowlist via `runner_env`,
prepends the repo root to `PYTHONPATH`, sets `PYTHONHASHSEED=0`, launches
`ndif.services.ray.sandbox.runner` with the socket path, `peers`, and — when the
pool has one — the model key, then polls until
the socket accepts (30s), raising if the process dies first. The runner's stderr
is never discarded: it is the only report of a request the runner could not
finish.

`Pool` keeps up to `size` runners pre-warmed on background threads (`refill`) so a
request doesn't pay interpreter + nnsight import + meta-model build; `acquire`
takes a warm one (spawning inline after 30s if none is ready) and tops the pool
back up. **There is no release-back-to-pool** — the caller owns the process and
must `stop()` it. `size` defaults to `NDIF_SANDBOX_POOL_SIZE` (7,
`src/ndif/services/ray/sandbox/model.py` `DEFAULT_POOL_SIZE`).

The default trades two costs. A cold spawn is roughly 6× a warm execution and
refills run concurrently, so the pool keeps up only if `size` is at least that
ratio; below it, every other request under load pays the spawn inline. But each
warm runner holds a few hundred MB idle (mostly fixed cost — modeling classes,
tokenizer, meta pipeline) and refills contend for the node's CPU, so the sandbox
path scales worse with replicas than the in-process one. Turn it down on a memory-
or core-tight node.

> **Gotcha:** a warm thread that fails to spawn is silent. `refill`'s `warm()` only
> decrements the counter in its `finally`; the exception dies with the thread and
> lands in the Ray worker's `.err` file, never the actor's log. The visible symptom
> is `acquire` waiting its full 30s and then spawning inline.

### The runner process

`Runner.__init__` (`src/ndif/services/ray/sandbox/runner.py`) takes the socket
path, `peers` and the model key, and calls `nns.load_meta_model(model_key)`
**before binding**, so a host that can connect is a host whose runner is warm.
A failed build is printed to
stderr and ignored: the map is an optimization, and without it ids resolve to
`None`. `__main__` first calls `die_with_parent`, a daemon thread that polls
`getppid` once a second and `os._exit(1)`s when the runner is reparented — the
actor was SIGKILLed (eviction, or an unrecoverable-CUDA restart) and no Python
cleanup ran. It polls rather than using `PR_SET_PDEATHSIG` because that signal
fires when the parent *thread* exits, and runners are spawned from short-lived
threads.

`Runner.serve` accepts `peers` connections (dropping `spawn`'s readiness probe via
`_hung_up`), handles the exchange, and loops forever; a failing exchange goes to
stderr without killing the loop. `Runner.handle` takes the payload from the first
host to connect, redirects stdout *and* stderr to separate `Writer`s (one `PRINT`
per complete line; stderr because `warnings.warn` goes there), and calls
`nns.run`.

`src/ndif/services/ray/sandbox/nns.py` `run` applies the request's env
(`set_env`), deserializes with
`BackendRequestModel.deserialize(..., unpickler=IPCCloudUnpickler)`, runs the block
through the in-process path's own `execute_traced_block` (same autocast bracket,
same save collection), and `torch.save`s the saved values with the shared
`src/ndif/services/ray/deployments/modeling/util.py` `cpu_pickle_module` — so the
host uploads the bytes as-is. Tracebacks don't survive serialization, so an
exception is **formatted here** (preferring the worker's `__intervention_tb__`)
and shipped as text with the terminal exception's class name.

### Persistent ids and the meta model

`IPCCloudUnpickler.persistent_load` resolves the payload's persistent ids in three
steps: `"Interleaver"` becomes an `IPCInterleaver` bound to this request's socket
(checked first, so the meta model's own interleaver can't shadow it); anything in
`PERSISTENT_OBJECTS` (`Module:<path>`, `Tokenizer`, `Pipeline`, …) resolves to the
runner's **meta** object, which lets a block call `model.tokenizer(...)` without a
round trip; anything else resolves to `None` rather than raising.

`load_meta_model` builds the map once per runner from
`Remotable.from_model_key(...)._remoteable_persistent_objects()` — the map the
in-process actor builds from its real model, so the keys line up. `set_env`
rebuilds it per request from the payload's `env`, because an env can reshape the
tree (a PEFT adapter prefixes every path with `base_model.model`). Both are
best-effort: a diverged tree degrades to ids resolving to `None`, since every
activation is a socket round trip anyway — so `ArchitectureMismatchError` is raised
on the in-process path only.

Importing `nns` installs process-wide patches, which is why `runner.py` imports it
and the host never does: `Mediator.event` → `ipc_event` (park untagged);
`Barrier.__call__` → `ipc_barrier`; every callable on `IPCEnvoy` grafted onto
`Envoy` itself (the tracer's root is a model-wrapper subclass that *inherits*
`interleave`); `Envoy.source` → `ipc_module_source` and `SourceEnvoy.source` →
`ipc_recursive_source`; and `InterleavingTracer.cache` → `ipc_cache`.

### The actor's seams

`SandboxModelDeployment` overrides only the seams of the `BaseModelDeployment.run`
template, so the timeout/cancel race, metrics, event logs and upload stay shared.
Each is on `SandboxHost` in `src/ndif/services/ray/sandbox/model.py` unless noted:

| Seam | Sandbox behavior |
|---|---|
| `execute` (`SandboxModelDeployment`) | trusted → base; else `run_in_runner(request, None)` |
| `execution_scope` | trusted → base capture; else `nullcontext` |
| `interrupt` (`SandboxModelDeployment`) | stop the runner, then the base `interrupt` |
| `format_error` | `RunnerError` → its text verbatim, non-fatal; else base |
| `error_name` | `RunnerError` → its shipped `cause_type`; else base |
| `cleanup` | `discard_sandbox`, then base |
| `restart` | `discard_sandbox`, then base (the actor is about to be killed) |
| `before_payload` / `after_payload` | no-ops here; the tensor-parallel actor tells its shards where the runner is, then releases them into the forward |

`error_name` makes a user mistake count under the same telemetry `error_type`
sandboxed or not. `run_in_runner`'s `seed` is `None` for one host; a
tensor-parallel group passes a shared one so every rank samples the same token.

## The split interleaver

Each nnsight `Mediator` is cut in half at the model↔mediator seam:

| Mechanism | Runner half | Host half |
|---|---|---|
| block execution / parking | worker greenlet; patched `Mediator.event` parks untagged with the pin | `MediatorProxy.adopt` tags `.i{n}` |
| occurrence counting, pin relaxation | none (pin pushed back on every `RESUME`) | inherited `Mediator.handle` on the proxy |
| forward hooks | `IPCInterleaver.instrument` is a no-op | the model's real `Interleaver` |
| batch scoping, input assembly | `batch_group` computed by the tracer, shipped | `Batcher` + `narrow`/`widen`, `SandboxDriver._assemble` |
| barriers | `ipc_barrier` parks every arrival | `SandboxDriver.barrier_arrival` counts and releases |
| `.source` / ad-hoc call / cache | `IPCSource`, `IPCEnvoy.__call__`, the real `Cache` | `install_source` + `serve_recursive_source`, `run_module`, `ShippingCache` |
| dangling workers | `IPCInterleaver.throw` | `check_dangling` sends `THROW` |

`MediatorProxy` (`src/ndif/services/ray/sandbox/driver.py`) **subclasses `Mediator`
and reuses `handle` unchanged** — matching, iteration and relaxation are stock
nnsight. It overrides `adopt` (re-tag an incoming park), `switch` (a greenlet hop
becomes a `RESUME`→`PARK` round trip), `start` (only records the interleaver — the
worker's first park arrived in `INTERLEAVE`) and `alive` ("has a pending park").

`IPCEnvoy.interleave` is the runner's entry into a model call: it prepends the
trace's edits, starts every worker so each parks on its first location, ships
`INTERLEAVE` with the raw per-invoke inputs, then hands the socket to
`IPCInterleaver.pump` until `DONE`. On the host, `SandboxDriver.interleave` builds
one proxy per park (`_build_proxies`, which settles their control parks), installs
them as the model interleaver's mediators, enters `request_dtype(dtype)` and the
interleaver, assembles the inputs (`_assemble`: trace-level kwargs win; host-built
tensors are moved by `_to_device`), runs `fn(...)` for real, serves the return
value at `"result"`, checks for dangling workers, and sends `DONE`.

### One read and one swap

```mermaid
sequenceDiagram
  participant F as forward hook (host)
  participant P as MediatorProxy (host)
  participant U as pump (runner)
  participant W as worker (runner)
  Note over W,P: worker parked on (VALUE, "model.h.0.output", pin)
  F->>P: handle("model.h.0.output", value)
  P->>P: tag matches ".i0" → narrow to batch_group
  P->>U: RESUME id, (narrowed,), pin
  U->>W: switch(narrowed) → worker parks on (SWAP, same loc, pin, edited)
  U->>P: PARK id, (SWAP, loc, pin, narrowed)  ← writeback of the read
  P->>U: widen(value, group, narrowed); RESUME id, (), pin
  U->>P: PARK id, (SWAP, loc, pin, edited)  ← the worker's real park
  P->>U: widen(value, group, edited); RESUME → worker parks elsewhere
  P-->>F: possibly-edited value returns into the forward
```

**Why the writeback.** A read hands the worker a *copy*, so an in-place edit
(`output[:] = 0`) would never reach the host. After resuming a worker that was
answering a read, `pump` sends a synthetic `SWAP` park at the same location
carrying the value the worker was handed (`IPCInterleaver.writeback`), while the
host is still inside `Mediator.handle` for that location — unconditionally, since
`pump` can't tell whether the object was edited. So every read also *replaces* the
activation: on a single-invoke trace `widen` returns the runner's copy verbatim,
and object identity is not preserved across a read. A read costs two round trips,
a read then a swap three; a location no worker is parked on never crosses.

### Host authority: occurrences, sources, barriers, caches

Everything that counts or decides lives on the host; the runner only parks. The
in-tree note's sections *Iteration authority*, *Source instrumentation* and
*Barrier authority* (`src/ndif/services/ray/sandbox/ARCHITECTURE.md`) give the
reasoning; the mechanics in brief:

- **Occurrences and `tracer.iter`.** The runner parks untagged with its pin
  (`ipc_event`); `MediatorProxy.adopt` tags with the pin when pinned, else with the
  proxy's own `iterations[location]`; every `RESUME` pushes the proxy's `iteration`
  back. A `RESUME` answering a control park or releasing a barrier sends the
  `KEEP_PIN` sentinel instead — those parks carry no pin, so the proxy's copy may be
  stale and the worker's own stands.
- **Control parks.** `MediatorProxy.settle_control` drains `SOURCE`/`CALL`/`CACHE`
  parks before `handle` sees them, once per proxy before the forward and again after
  every `switch`. `run_module` defers to the host `Envoy.__call__`, so `hook=False`
  spends no occurrence for the module or anything under it.
- **Recursive `.source`.** `ipc_recursive_source` arms the host with a SOURCE park
  carrying the op path, then parks on `{path}.fn`. When the op fires,
  `SandboxDriver.serve_recursive_source` instruments the live callable host-side and
  serves a *description* (`{names, lines, source}`) or `(SOURCE_ERROR, message)` with
  the base `SourceNotAvailable` wording; the callable never crosses. Arming that came
  with the initial parks is re-applied after `Interleaver.__enter__` clears
  `sourced`.
- **Barriers.** `ipc_barrier` parks every arrival (`Event.BARRIER`);
  `SandboxDriver.barrier_arrival` counts per barrier id and releases the round when
  full. Counting runner-side would release workers the host never hears about.
- **Caches.** `ipc_cache` builds an ordinary nnsight `Cache` in the runner and parks
  on `CACHE`; the host attaches a `ShippingCache` whose `_record` sends `CACHE_HIT`
  for every location the forward reaches, narrowed to the worker's rows.

## Termination

| Path | Trigger | Mechanism |
|---|---|---|
| Normal | block finishes | forward returns → `DONE` → `pump` returns → block ends → `END` with the saved-values blob → host uploads it |
| `tracer.stop()` | `EarlyStopException` in a worker | `pump` sends `STOP` and re-raises; the proxy's `switch` raises `EarlyStopException` on the host, unwinding the forward, which `Interleaver.__exit__` swallows; `IPCEnvoy.interleave` swallows it in the runner too |
| Dangling worker | worker still parked after the forward | `check_dangling` classifies the park as nnsight's `dangling_unwind` does and sends `THROW`: `OUT_OF_ORDER` raises `OutOfOrderError`, `ITER` warns that a `tracer.iter` loop outran the model, `BARRIER` raises the base `ValueError`. A fatal kind makes the runner report `EXCEPTION` and exit, so the `THROW` loop and the `DONE` send tolerate `OSError` and let `pump` read the buffered `EXCEPTION` |
| Block error | any exception in the runner | formatted in `nns.run`, sent as `EXCEPTION`; `next_event` raises `RunnerError(text, cause_type)`; `format_error` returns the text, non-fatal; `error_name` records `cause_type` |
| Host error | the forward raises on the host (a swapped value the next layer can't use, a widen that can't splice an edit) | the host's own exception propagates to the base `format_error` as itself; the runner's recv fails and its noise stays in the runner's stderr |
| Timeout / cancel | `BaseModelDeployment.run`'s race | `SandboxModelDeployment.interrupt` calls `Sandbox.stop` (SIGTERM, SIGKILL after 5s; it blocks the actor's event loop meanwhile), then the base `interrupt`, which calls `src/ndif/services/ray/deployments/modeling/util.py` `kill_thread` to inject `SystemExit` into the recorded execution thread. The injection is asynchronous: it fires at that thread's next bytecode boundary, and the thread is dedicated to the request (`run_on_own_thread`), so it can only land in this request or nowhere. The host thread's socket `recv` fails with `ConnectionError` once the runner is gone |
| Always | after every request | `cleanup` → `discard_sandbox` stops the request's runner; `restart` does the same before the actor is killed; `die_with_parent` covers a SIGKILLed actor |

`next_event` services `PRINT` throughout — it echoes the line as a `Status.LOG`
response and keeps reading — so a caller waiting for a specific reply never sees
the user's stdout.

## What the separation is

**Process separation, not a security boundary.** What it provides:

- The block executes in a different OS process and reaches the actor only through
  the message catalog — it has no handle on the actor's Python objects, its
  interleaver, or `self.model`.
- The process is fresh per request (`Pool.acquire` hands out a runner that has
  never run user code; `cleanup` → `discard_sandbox` stops it), so compiled
  blocks, globals, imported-module state and leftover objects do not carry between
  requests.
- The environment is an allowlist, not a copy: `runner_env` passes through only the
  names in `RUNNER_ENV` (`src/ndif/services/ray/sandbox/host.py`) — loader and
  locale basics, `CUDA_VISIBLE_DEVICES`, cache locations, thread-count knobs. So
  `HF_TOKEN`, `NDIF_INFLUX_TOKEN`, `NDIF_POSTGRES_URL` and object-store credentials
  are **not inherited**. It also drops `RANK`, `LOCAL_RANK`, `WORLD_SIZE`,
  `MASTER_ADDR` and `MASTER_PORT`, so a runner spawned from rank 0 doesn't claim to
  be rank 0 of a group it is not in.

What it does not provide: anything an OS-level sandbox would. The runner is a plain
`subprocess.Popen` child — same uid, filesystem, network and GPUs, no namespaces,
seccomp, rlimits or filesystem jail — so what is reachable by those means (another
process's `/proc/<pid>/environ`, a token file in the HF cache, the Ray client port)
is not shown to be out of reach. It is a seam hardening can be added behind.

## What the untrusted path cannot do

The runner holds a **meta** model — structure, no weights — and the backward pass
runs on the host after the forward. So on the untrusted path today:

- **Weight reads** (`model.lm_head.weight`) resolve to the runner's meta parameter:
  it has a shape and no data, and arithmetic with it against real activations
  fails.
- **Weight writes** land on the meta copy and are a no-op on the host's model.
- **Training loops** over parameters (an optimizer stepping `model.parameters()`)
  update meta tensors and are a no-op.
- **Gradients** (`.grad` inside `with loss.backward():`) are unsupported: the
  runner's worker is parked on a location the model has already passed, and the
  request fails with `OutOfOrderError`.
- **`eproperty` write-back transforms don't fire.** A transform is bound onto the
  mediator in the runner, whose `handle` never runs, so only the generic writeback
  of the raw value reaches the host.

Activation work — reads, swaps, in-place edits, `.source`, ad-hoc module calls,
caches, barriers, `tracer.iter`, `tracer.stop()` — crosses the socket and is served
by the host's own nnsight machinery, which is what keeps it aligned with the
in-process path.

## Gotchas

- **Both sides autocast.** The runner brackets the block and `SandboxDriver`
  brackets the forward it drives, each at the model's dtype (`request_dtype`).
  Without the host half the model's own arithmetic runs uncast and untrusted
  results diverge from trusted ones.
- **Activations land on the receiver's device.** `torch.load`'s `map_location`
  places them as they are rebuilt (`SandboxDriver.pump` sets it); `_to_device`
  covers the tensors the batcher and tokenizer build on the host, which never cross
  the wire. Assuming the sender's device breaks tensor parallelism: one runner
  serves a whole group, so a value rebuilt from rank 0's bytes would reach rank 1
  labelled `cuda:0`.

## Related

[Sandbox execution](../concepts/sandbox-execution.md) is the mental-model version
of this page. [Model actor](model-actor.md) covers `BaseModelDeployment` and its
`run` template; [nnsight integration](nnsight-integration.md) serialization and the
client contract; [Request lifecycle](../concepts/request-lifecycle.md) how a
request reaches `execute`; [Error messages](../reference/error-messages.md) what
`RunnerError` / `OutOfOrderError` mean.
