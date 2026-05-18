# NDIF user-experience report

This document captures the exact strings and behaviors a remote nnsight
user observes against the NDIF stack on the `features/replicas` branch.
Every claim here is backed by a test in this directory.

How to reproduce: bring the stack up (`make ta`), then from the repo
root run

```bash
pytest tests/new/ --run-remote -v
```

Tests are organized so each file is independently runnable.

The test stack runs with accelerated knob values (see
`docker/docker-compose.yml`):

```
NDIF_AUTOSCALING_INTERVAL_S       = 1
NDIF_AUTOSCALING_WAIT_THRESHOLD_S = 5
NDIF_AUTOSCALING_BACKOFF_S        = 10
NDIF_STATUS_CACHE_FREQ_S          = 10
```

Production defaults are 5 / 30 / 120 / 10. Substitute accordingly when
reading "happens within ~5s" type claims below.

---

## 1. Happy-path lifecycle (`test_basic_usage.py`, `test_request_lifecycle.py`)

A normal warm trace produces exactly this sequence of `(status,
description)` pairs over Socket.IO:

```
RECEIVED    "Your job has been received and is waiting to be queued."
QUEUED      "Added to Queue at position N."
DISPATCHED  "Your job has been sent to the model deployment."
RUNNING     "Your job has started running."
COMPLETED   "Your job has been completed."
```

The QUEUED frame's `N` is the request's position; for an idle warm
replica it's always `1`.

`print(...)` inside a remote trace body produces an extra
`LOG` frame carrying the captured stdout:

```
LOG  "<captured stdout from print()>"
```

User exceptions terminate the sequence with:

```
ERROR  "<formatted traceback including exception message>"
```

### Cold start (model not yet deployed)

The user-visible status stays at `QUEUED` while the Processor walks
through `PROVISIONING → DEPLOYING → READY`. Only the QUEUED frame's
description changes:

```
RECEIVED    "Your job has been received and is waiting to be queued."
QUEUED      "Model Provisioning..."        ← Processor.status = PROVISIONING
QUEUED      "Model Deploying..."           ← Processor.status = DEPLOYING
QUEUED      "Moved to position 1 in Queue." ← Processor READY, queue draining
DISPATCHED  "Your job has been sent to the model deployment."
RUNNING     "Your job has started running."
COMPLETED   "Your job has been completed."
```

The number of `QUEUED` frames is variable (each stage transition emits
one), but the description literals above are the canonical ones.

---

## 2. Eviction-while-in-flight matrix (`test_eviction_user_experience.py`)

The user's last-frame status and description for each scenario:

| Request stage when evicted | Eviction kind | Final status | Description (literal) |
|---|---|---|---|
| in QUEUE (pre-dispatch) | fan-out | `ERROR` | `"Replica evicted before dispatch. ..."` or `"Model deployment evicted. ..."` (see below) |
| DISPATCHED / RUNNING (in-flight) | fan-out | `ERROR` | `"Replica was evicted while processing your request. ..."` or, on a tight race past `handle()`, `"Error submitting request to model deployment. ..."` |
| between requests (no in-flight) | fan-out | `COMPLETED` (next cold-start succeeds) | normal lifecycle from §1 |
| any | targeted (1 of N siblings) | usually `COMPLETED` (next dispatch lands on a sibling); racy if the busy replica is the one targeted |

These four string literals are the canonical eviction-related strings
the user can see:

```
"Replica evicted before dispatch. Sorry for the inconvenience. Please try again later."
"Replica was evicted while processing your request. Sorry for the inconvenience. Please try again later."
"Error submitting request to model deployment. Please try again later. Sorry for the inconvenience."
"Model deployment evicted. Please try again later. Sorry for the inconvenience."
```

The first three originate in `Replica.dispatch` (per-replica state);
the fourth originates in `Processor.on_replica_exit` (last-replica
tear-down). `TestDocumentedStrings` in
`test_eviction_user_experience.py` greps the source so any rewording
without a report.md update will fail loudly.

---

## 3. Hotswap / cache transitions (`test_hotswapping.py`)

State machine traversal on the 2-GPU test stack (compose `device_ids:
["6", "7"]`):

- `COLD → HOT` — first request to an undeployed model lazily deploys
  it. After the trace returns, `/status` reports `HOT`.
- `HOT → WARM` — deploying a model that needs both GPUs (e.g.
  Llama-3.1-70B) forces smaller HOT models off the GPU. The CPU cache
  has plenty of room for them, so they demote to `WARM` rather than
  being deleted.
- `WARM → HOT` — re-tracing a model that's in `WARM` cache reloads it
  from CPU memory (no disk reload). The user just sees the normal
  trace sequence.
- `HOT → WARM` (single-replica targeted evict) — `evict --replica
  <rid>` keeps the replica's `replica_id` and demotes it to `WARM`
  (assuming CPU cache has room).
- `HOT → ∅` (fan-out evict) — `evict <repo>` with no `--replica`
  removes every HOT and WARM replica of the model.

Pinned deployments (`deploy --pinned` or scheduled via the dashboard)
are never evicted to make room for another model. `TestPinnedProtection`
verifies that a small pinned model survives queue pressure from a
non-pinned, larger model.

---

## 4. Multi-replica behavior (`test_replicas.py`)

- `deploy --replicas N` places `N` replicas with unique `replica_id`s
  on first call.
- Deploy is *additive*: a second call with `--replicas 1` adds one more
  replica rather than replacing an existing one.
- Each replica's Ray actor name is `{replica_id}:ModelActor:{model_key}`
  in the `NDIF` namespace.
- Per-replica `evict --replica <rid>` removes only that replica;
  siblings remain HOT.
- Per-replica `restart --replica <rid>` kills the actor and the
  controller respawns it under the *same* `replica_id` (the slot is
  preserved). Siblings are untouched.
- Two concurrent traces against a 2-replica deployment run in parallel,
  not serially — the Processor's queue feeds whichever Replica is free.
- Pinned multi-replica works (`deploy --replicas 2 --pinned` produces
  two pinned slots, neither evictable).

---

## 5. Autoscaling (`test_autoscaling.py`)

The Processor watches the head of its queue every
`NDIF_AUTOSCALING_INTERVAL_S` seconds. If `time.time() -
head.enqueued_at > NDIF_AUTOSCALING_WAIT_THRESHOLD_S`, it asks the
controller for one more replica, then sleeps
`NDIF_AUTOSCALING_BACKOFF_S` before considering another scale-up.

Observed behavior:

- **Light load** (sequential traces, each completes well under
  `WAIT_THRESHOLD_S`): never scales up. Stays at the initial replica
  count.
- **Sustained pressure**: a single long-running request that blocks the
  Replica for > `WAIT_THRESHOLD_S` causes the autoscaler to add one
  replica within `INTERVAL_S + WAIT_THRESHOLD_S + dispatch latency`.
- **Backoff**: after a scale-up, no further replicas are added during
  the `BACKOFF_S` window even if pressure persists. Verified by
  spawning 4 long requests against 1 replica and confirming exactly
  one scale-up within 10s.
- **CANT_ACCOMMODATE**: when the cluster has no room for an additional
  replica, the autoscaler logs the failure and the Processor continues
  serving with its existing pool. No user-visible error from the
  autoscaler itself (the request that triggered it will see the
  ordinary queue-position updates).

Autoscaling does *not* introduce any new status code on the wire.
Users in the queue while it's deciding just see the standard
`QUEUED "Moved to position N in Queue."` updates as the new replica
spins up and starts draining.

---

## 6. Dispatcher robustness (`test_dispatcher_robustness.py`)

Two recovery paths:

1. **Reconcile event** — `cli.lib.deploy`, `cli.lib.evict`, and the
   dashboard's deploy/evict endpoints publish a `reconcile_model`
   event on the `dispatcher:events` Redis stream after a successful
   controller mutation. The Dispatcher routes the event to the
   matching Processor which diffs its pool against
   `Controller.get_deployment(model_key)` and adjusts.
2. **Drift detection** — if a reconcile event is dropped or never
   published (e.g. `ray.kill()` invoked directly against a ModelActor),
   the next `Replica.dispatch` call raises `Failed to look up actor`.
   The Replica flips `self.dropped`, exits its worker loop, and
   `on_replica_exit` removes it from the Processor's pool. If it was
   the last live Replica, the Processor aborts with the documented
   "Model deployment evicted" string to any queued users.

Lazy Processor creation is preserved: the Dispatcher does not
pre-allocate Processors. The first request for a fresh `model_key`
spawns the Processor, which then drives `PROVISIONING → DEPLOYING →
READY`.

`notify_reconcile` is best-effort: a broker outage logs a warning but
does not break the deploy/evict that just succeeded on the controller.

---

## 7. Test inventory

| File | Coverage |
|---|---|
| `test_basic_usage.py` | trace + save, generate, non-blocking, ERROR path |
| `test_request_lifecycle.py` | exact strings for each status, cold vs warm sequence, LOG path, queue position |
| `test_hotswapping.py` | HOT/WARM/COLD transitions, per-replica vs fan-out evict, pinned protection |
| `test_replicas.py` | multi-replica deploy/evict/restart, actor naming, concurrent serving |
| `test_autoscaling.py` | no scale-up under light load, scale-up under pressure, backoff, capacity exhaustion |
| `test_eviction_user_experience.py` | the eviction × in-flight-stage matrix, plus source-string lock-in |
| `test_dispatcher_robustness.py` | reconcile events, drift detection, lazy Processor creation, notify best-effort |

---

## 8. Discussion — rough edges worth addressing

Behavior the test suite captured but that may merit a follow-up change.
Listed roughly in order of impact on end users.

### 8.1. Three different error strings for "model was evicted under you"

`test_evict_during_running` exercises the in-flight eviction path and
observes one of three terminal `ERROR` descriptions depending on which
race the eviction loses to:

- `"Replica was evicted while processing your request. ..."` — clean
  `CancelledError` path inside `Replica.dispatch`.
- `"Model deployment evicted. ..."` — last-replica tear-down via
  `Processor.on_replica_exit`.
- `"Error submitting request to model deployment. ..."` — generic
  fallback when `handle.__call__.remote(request)` raises something
  other than `Failed to look up actor`.

The first two carry the cause; the third looks like an arbitrary
server error to the user. A user seeing it would have no signal that
their model was evicted by an admin and that a simple retry is
appropriate. Consolidating to one consistent "evicted, please retry"
description (and ideally a structured error code the client can
auto-retry on) would simplify both UX and any future retry logic in
nnsight's `RemoteBackend`.

### 8.2. Cold-start-after-evict race (~12s window)

The Dispatcher's `brpop` has a 10s timeout, and only after that timeout
does it drain its eviction queue and purge cancelled Processors. A
user who retries within ~10s of an evict lands on the just-cancelled
Processor and sees `"Model deployment evicted"` instead of cold-starting
cleanly.

The test suite works around this by sleeping 12 s after every eviction
in fixtures (`tests/new/test_request_lifecycle.py` `_evict_first`,
`tests/new/test_eviction_user_experience.py` `TestEvictionBetweenRequests`,
etc.). That's a smell: real users won't sleep 12 s before retrying.

Two reasonable fixes:
- Tighten the eviction-queue drain (event-driven rather than tied to
  the `brpop` timeout).
- Add auto-retry-on-evicted to nnsight's `RemoteBackend` so the user
  doesn't see the error at all in this window.

### 8.3. Per-replica evict is unsafe under load

`evict --replica <rid>` can target a Replica that's currently serving
a request. The controller doesn't track per-Replica busy state (that
lives in the Dispatcher), so neither the CLI nor the dashboard has a
way to safely target the "idle" sibling. When the busy one gets hit
the in-flight user request dies with one of the §8.1 strings.

The dashboard's "Evict one" / "Restart one" buttons hit this directly.
A `drain=True` flag on the evict path (Replica waits for
`current_request is None` before tear-down, with a timeout) would
make these operations safe to invoke from the UI without coordinating
with whoever's currently using the model.

### 8.4. `PROVISIONING` and `DEPLOYING` are descriptions, not statuses

During cold start the user-visible status stays `QUEUED` and only the
description changes (see §1, "Cold start"). Clients that want to
render "your model is being deployed" differently from "you're third
in the queue" have to parse the description string.

Surfacing them as first-class `BackendResponseModel.JobStatus` values
(or at least a structured `stage` field on the response) would let
the dashboard and nnsight's client both render distinct UI states
without string parsing.

### 8.5. No scale-DOWN path

`Processor.autoscaling_loop` only scales up. Once added, extra
replicas linger until something else evicts them — another model
needing the room (only works if they're non-pinned and past
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`) or an explicit admin evict.

This is probably intentional — warm replicas are valuable and the
cost of carrying one is low compared to a fresh provision — but it's
worth being explicit about in `NDIF.md`. A heuristic scale-down
("after `IDLE_THRESHOLD_S` of empty queue, drop one replica") would
keep replica counts proportional to actual demand.

### 8.6. Targeted-evict flakiness (minor)

`test_evict_one_leaves_siblings` passed on rerun after failing once
in the first full-suite run. The replica tracking eventually
converged but the moment-of-eviction state visible via `/status` is
briefly inconsistent. Probably just a status-cache vs controller-state
race, but worth a closer look if the dashboard ever polls `/status`
right after issuing an evict.

### Priority

If only two of these are worth doing now, I'd pick 8.1 (consistent
eviction string) and 8.3 (drain-before-evict). Both directly affect
what users and operators see in normal operations; the others are
either rare-race (8.2, 8.6) or cosmetic (8.4) or
design-by-omission (8.5).
