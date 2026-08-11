# NDIF — Agent Guide

NDIF is the server behind [nnsight](https://nnsight.net): it runs user-submitted
intervention code against large models on shared GPUs, so researchers can inspect
model internals they can't host themselves.

This file is a **router**. The content lives in `docs/`. Find the user's intent
below, open that page, and read it before writing code or running commands — the
docs are recipe-style, cite `file:line`, and are kept in sync with the
implementation.

If you're new to this repo, read [docs/concepts/request-lifecycle.md](docs/concepts/request-lifecycle.md)
once. It's the keystone: every other page is a zoom into one of its hops.

---

## Read this before anything else

**Where user code runs depends on one boolean.** `request.trusted` is stamped at
ingress from the API key. A trusted block runs **inside the model actor process**,
next to the weights; an untrusted one runs in a **separate runner process** driven
over a Unix socket. **With auth off — no `NDIF_POSTGRES_URL` — a client-supplied
`trusted` is honored and an unspecified one defaults to trusted**, and a trusted
model loads with `trust_remote_code`. A bare `just up` is therefore
unauthenticated *and* runs user code in-process by default.

Two consequences that will mislead you if you don't know them:

- A local stack **doesn't exercise the sandbox path by default** — auth-off
  defaults to trusted, *and* the code-default actor class is the in-process one
  (only compose sets `SandboxModelActor`). But you no longer need Postgres to
  reach the untrusted path: send `trusted: false` in the request and auth-off
  honors it, forcing the runner path; see
  [docs/developing/testing.md](docs/developing/testing.md).
- Sandboxing is **process-based and still in progress**. The runner is an ordinary
  OS process with no hardening; each request gets a fresh one, stopped afterward,
  so nothing leaks between requests. Don't describe it as a security boundary.

---

## By task

### "Get NDIF running / I'm setting up my own"
- [docs/operating/quickstart.md](docs/operating/quickstart.md) — `just up` to a working `remote=True` trace
- [docs/operating/compose-stack.md](docs/operating/compose-stack.md) — the ten containers, ports, volumes, GPU requirements
- [docs/operating/configuration.md](docs/operating/configuration.md) — env-only config and how it layers
- [docs/operating/production.md](docs/operating/production.md) — what must change before users can reach it

### "Deploy / evict / size a model"
- [docs/operating/models-and-deployment.md](docs/operating/models-and-deployment.md) — model keys, the ways to deploy, every per-model field
- [docs/runbooks/deploy-and-pin-a-model.md](docs/runbooks/deploy-and-pin-a-model.md) — the procedure
- [docs/runbooks/model-oom-on-deploy.md](docs/runbooks/model-oom-on-deploy.md) — when it won't fit
- [docs/concepts/deployments-and-eviction.md](docs/concepts/deployments-and-eviction.md) — HOT/WARM/COLD, pinning, GPU accounting

### "Run a command against the cluster"
- [docs/operating/cli.md](docs/operating/cli.md) — every `ndif` command with flags and real output
- [docs/operating/dashboard.md](docs/operating/dashboard.md) — the admin UI, its auth, its crons

### "Something is broken"
- [docs/operating/troubleshooting.md](docs/operating/troubleshooting.md) — stack-level triage, start here
- [docs/errors/client-side-failures.md](docs/errors/client-side-failures.md) — a user pasted an nnsight error
- [docs/errors/server-exceptions.md](docs/errors/server-exceptions.md) — you're reading server logs
- [docs/runbooks/debug-a-stuck-request.md](docs/runbooks/debug-a-stuck-request.md) — a hung job, end to end
- [docs/runbooks/trace-a-users-failed-job.md](docs/runbooks/trace-a-users-failed-job.md) — reconstruct a past failure

### "Secure it / turn on auth"
- [docs/runbooks/enable-auth.md](docs/runbooks/enable-auth.md) — the procedure, and what you're exposed to until you run it
- [docs/concepts/auth-and-limits.md](docs/concepts/auth-and-limits.md) — what is and isn't enforced

### "Watch it / find the logs and metrics"
- [docs/operating/observability.md](docs/operating/observability.md) — Loki, InfluxDB, Prometheus, Grafana
- [docs/developing/telemetry-internals.md](docs/developing/telemetry-internals.md) — every metric and where it's emitted

### "Grow the cluster"
- [docs/runbooks/add-a-gpu-node.md](docs/runbooks/add-a-gpu-node.md) — join a second GPU machine
- [docs/developing/ray-service.md](docs/developing/ray-service.md) — head vs worker, ports, node resources

### "Understand how a request actually runs"
- [docs/concepts/request-lifecycle.md](docs/concepts/request-lifecycle.md) — the keystone
- [docs/concepts/sandbox-execution.md](docs/concepts/sandbox-execution.md) — why user code runs in another process
- [docs/developing/sandbox-internals.md](docs/developing/sandbox-internals.md) — the wire protocol and the split interleaver

### "Change the server's code"
- [docs/developing/index.md](docs/developing/index.md) — the internals tree
- [docs/developing/architecture-overview.md](docs/developing/architecture-overview.md) — the process map and boundaries
- [docs/developing/repo-layout.md](docs/developing/repo-layout.md) — "I want to change X, open Y"

### "Extend it"
- [docs/developing/adding-a-model-actor.md](docs/developing/adding-a-model-actor.md) — a custom execution path
- [docs/developing/adding-a-provider.md](docs/developing/adding-a-provider.md) — a new backing service
- [docs/developing/adding-a-service.md](docs/developing/adding-a-service.md) — a fourth service container

### "The user's question is really about the client"
nnsight owns the client side — API keys, `remote=True`, non-blocking jobs, what
`.save()` does. nnsight is a normal installed dependency here (in
`requirements.txt`, and bind-mounted from a local checkout for dev), but its
agent docs (`CLAUDE.md` plus `docs/`) live in its own repo and **are not
guaranteed to be present in this checkout**. So don't assume a local docs path;
when they aren't here, work from [nnsight.net](https://nnsight.net) and the
[nnsight repo](https://github.com/ndif-team/nnsight).

---

## By subsystem

| Subsystem | Source | Doc |
|---|---|---|
| HTTP ingress | `src/ndif/services/api/` | [api-service.md](docs/developing/api-service.md) |
| Queue + autoscaling | `src/ndif/services/api/queue/` | [queue-internals.md](docs/developing/queue-internals.md) |
| Ray node | `src/ndif/services/ray/` | [ray-service.md](docs/developing/ray-service.md) |
| Placement + eviction | `.../deployments/controller/` | [controller-internals.md](docs/developing/controller-internals.md) |
| Model execution | `.../deployments/modeling/` | [model-actor.md](docs/developing/model-actor.md) |
| Untrusted execution | `.../ray/sandbox/` | [sandbox-internals.md](docs/developing/sandbox-internals.md) |
| Backing services | `src/ndif/common/providers/` | [providers.md](docs/developing/providers.md) |
| Redis conventions | `src/ndif/common/redis/` | [redis-layer.md](docs/developing/redis-layer.md) |
| Wire models | `src/ndif/common/schema/` | [schemas.md](docs/reference/schemas.md) |
| CLI | `src/ndif/cli/` | [cli-internals.md](docs/developing/cli-internals.md) |
| Dashboard | `src/ndif/services/dashboard/` | [dashboard-internals.md](docs/developing/dashboard-internals.md) |

---

## Concepts

Read the first two if a behavior seems inexplicable:

- [request-lifecycle.md](docs/concepts/request-lifecycle.md) — one remote trace, every hop
- [services-and-topology.md](docs/concepts/services-and-topology.md) — what talks to what
- [queue-and-scheduling.md](docs/concepts/queue-and-scheduling.md) — one Redis list, per-model queues
- [deployments-and-eviction.md](docs/concepts/deployments-and-eviction.md) — what "deployed" means
- [sandbox-execution.md](docs/concepts/sandbox-execution.md) — the trusted/untrusted fork
- [status-and-results.md](docs/concepts/status-and-results.md) — statuses and result blobs
- [auth-and-limits.md](docs/concepts/auth-and-limits.md) — keys, and what isn't enforced

---

## Reference

- [http-api.md](docs/reference/http-api.md) — every endpoint
- [schemas.md](docs/reference/schemas.md) — every wire field and the Status enum
- [redis-keys.md](docs/reference/redis-keys.md) — every key, channel, stream
- [env-vars.md](docs/reference/env-vars.md) — every `NDIF_*` variable
- [ports.md](docs/reference/ports.md) — every port
- [glossary.md](docs/reference/glossary.md) — the vocabulary
- [external-resources.md](docs/reference/external-resources.md) — where to go next

---

## Inline gotcha cheat-sheet (read before touching NDIF)

- **Auth off ⇒ trusted by default, not unconditionally.** No `NDIF_POSTGRES_URL`
  means unauthenticated, and an unspecified `trusted` defaults to in-process next
  to the weights *with* `trust_remote_code`. But a client-supplied `trusted` is
  honored — send `trusted: false` to force the sandbox path with no Postgres.
- **NDIF does not use Ray Serve.** Deployments are detached Ray actors named
  `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace. The README says
  otherwise; the README is wrong.
- **You cannot catch an actor's exception by type across the Ray boundary.** An
  exception raised inside an actor reaches the caller wrapped in a
  `RayTaskError`; the dual class that would make `isinstance` work is only built
  when Ray applies `as_instanceof_cause()`, and over Ray Client — how the
  dispatcher connects — it does not. Read `.cause` (the eviction check in
  `queue.replica.Replica.dispatch`). A bare `isinstance` silently matches
  nothing, which is how a whole retry path can be dead while every log line
  looks correct. The exception is `ActorUnavailableError` — Ray raises that
  itself, so it *does* arrive bare and is matched directly.
- **The sandbox runner pool costs memory per model actor.** `NDIF_SANDBOX_POOL_SIZE`
  (7) pre-warms that many runners per actor at ~420 MB each — ~2.9 GB per
  resident model, whether or not anything is running. Sized for throughput on
  one model; turn it down on a node hosting several.
- **`NDIF_MODEL_CACHE_PERCENTAGE` scales host RAM**, not GPU memory — it's the WARM
  cache budget. Wrong lever for a GPU OOM.
- **There is no execution timeout by default.** `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS`
  is unset, so a block runs until it finishes and holds its replica for the
  duration. Right for a single-user box, wrong for a shared one — set it (or a
  per-model `execution_timeout_seconds`) before other people can submit.
- **`priority` is a strict group, not a queue jump, and there is no aging.**
  Priority requests sort ahead of all normal traffic and stay FIFO among
  themselves; sustained priority load starves normal traffic indefinitely, with
  autoscaling (max 3 replicas) the only relief. `prepend` is now only for
  re-queueing an evicted request to the front of *its own* group.
- **`NDIF_RAY_METRICS_PORT` is Ray's `--metrics-export-port`** (the Prometheus
  scrape target, default 8080). Nothing to do with Ray Serve. Formerly
  `NDIF_RAY_SERVE_PORT` — renamed, no alias.
- **The API process can't reach Ray.** Only the dispatcher — a child of the gunicorn
  master — holds a Ray client. Endpoints read Redis caches instead.
- **`ray:connected` has no TTL**, so `/ping`, `/connected`, `/status` and `/env`
  keep reporting healthy after the dispatcher dies.
- **Redis is the only durable handoff.** Once popped, a request lives in dispatcher
  memory; an API restart drops in-flight work silently.
- **Inside the `ray` container, `localhost:6379` is Ray's GCS, not Redis.** The
  effective Ray head port is `6385` via the CLI; `start.sh`'s bare fallback of
  `6379` collides with Redis.
- **Presigned result URLs are signed with `NDIF_OBJECT_STORE_PUBLIC_URL`** and
  expire after an hour. Sign with an address the *client* can reach, or jobs
  complete and then fail to download.
- **The dashboard SPA is committed** — `frontend/dist/` is checked in, so a clean
  clone + `just up` serves the UI with no host-side build. Rebuild only if you
  change the frontend.
- **CLI deploys can now be trusted and set `dtype`.** `ndif deploy` has `--trusted`
  and `--dtype` flags, and `models.yaml` passes every `DeploymentConfig` field
  (`trusted`, `dtype`, `padding_factor`, `execution_timeout_seconds`,
  `envoy_class`, `model_key`).
- **Only `dashboard_data` persists.** Result blobs, metrics, logs, Postgres data and
  downloaded weights all vanish on `just down`.
- **A single-GPU model must be loaded with `device=`, not a device map.** Models
  load through `transformers.pipeline`, and when a device map resolves to *one*
  device the pipeline factory puts the model on `cuda:0` whatever the map said —
  `device_map="balanced", max_memory={2: ...}` and `device_map={"": 2}` both land
  every tensor on cuda:0 (measured, transformers 5.15), while the same maps go to
  cuda:2 through `AutoModel.from_pretrained`. Invisible while every model gets
  card 0; the moment one doesn't, the actor refuses to start with "is on cuda:0,
  outside the assigned set [2]". Multi-card `device_map="balanced"` is fine, and
  so is the WARM restore, which goes through accelerate directly.
- **Tensor parallelism needs transformers >= 5.15.** Older versions don't shard a
  tied LM head's weight while still gathering its output, so any model with
  `tie_word_embeddings=True` serves logits `tp_size` times too wide — with a
  correct argmax inside the first copy, so only the width gives it away.
  `requirements.txt` carries the floor and nnsight refuses to shard below it.
- **Telemetry providers connect at import and own threads** — connect them after
  forking, or you get no formatting and no telemetry, silently.

---

## Working on this repo

```bash
just up            # build + start the whole stack, detached
just ta            # down -> rebuild -> up (source is baked in; needed after a code change)
just logs api      # follow one service
just ps            # what's running
pytest tests/      # the live-server suite (skips unless localhost:8001 is up)
```

There is no CI. The only test suite requires a running stack. See
[docs/developing/testing.md](docs/developing/testing.md) and
[docs/developing/contributing.md](docs/developing/contributing.md).
