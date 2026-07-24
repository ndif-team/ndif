---
title: Contributing
one_liner: House style and conventions this codebase actually follows — comments, docstrings, env vars, logging, where code belongs, and commit form.
tags: [internals, dev]
related: [docs/developing/testing.md, docs/developing/repo-layout.md, docs/developing/adding-a-provider.md, docs/developing/adding-a-service.md, docs/developing/telemetry-internals.md, docs/reference/env-vars.md]
sources: [src/ndif/common/providers/base.py, src/ndif/common/providers/loki.py, src/ndif/common/telemetry.py, src/ndif/common/redis/__init__.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/sandbox/ARCHITECTURE.md, pyproject.toml]
---

# Contributing

## What this covers

There is no `CONTRIBUTING.md`, no CI, no lint gate, and no PR template in this
repo. What there *is* is a strong and consistent house style visible in the source
itself. This page states it explicitly so you can hold the bar without reverse-
engineering it. Everything below is inferred from the code; where the code and a
comment disagree, the code wins and the comment is a bug.

## Comments and docstrings

**The module docstring is where the concept gets taught.** This codebase is
unusually good at this and it is the single most valuable convention in it. A
module docstring here doesn't say "this module contains the Redis provider" — it
states the design constraint and then the mechanism. Compare
`common/providers/loki.py:1`, which explains *why* Loki shipping is opt-in and
lazy before it explains what the handler does, or `common/metrics.py:15`, which
teaches InfluxDB's tags-vs-fields split and the cardinality reasoning behind every
choice in the file. Match that. A new module in `common/` or a new subsystem
without a teaching docstring is incomplete.

Below the module level the pattern is three layers, same as the rest of the
codebase:

- **Module docstring** — the concept, the constraint, the shape.
- **Class/function docstring** — the contract and the reasoning. Note the
  *reasoning*: `RedisProvider.connect` (`common/providers/redis.py:31`) spends its
  whole docstring on why `socket_timeout=None` is passed explicitly, because
  someone will otherwise delete it.
- **Inline comment** — why *this* implementation, never what the line does.

**Comment the why, not the what.** Concretely, the comments that earn their place
here name a failure mode: "without this they'd default to localhost:6379 — which
inside this container is Ray's own GCS", "threads don't survive `fork()`, so a
thread created in the master would be dead in every forked worker", "a silent
'auth off' would be a security hole". Comments that restate the line get deleted.

**The source is present tense.** No "this used to", no issue numbers, no
`TODO`/`FIXME`, no comment about the change you are making — describe what the
code does, not the bug you were fixing. Git holds the history; the commit message
is where the before/after belongs.

**Big subsystems get a design doc.** `src/ndif/services/ray/sandbox/ARCHITECTURE.md`
is the model: 250 lines explaining the interleaving split, with a table of files,
a diagram, and an explicit "current simplifications" section. If you build
something with that much internal structure, write one next to the code and keep
the docstrings pointing at it.

## Configuration and env vars

**Every knob is an environment variable, and there is no config file.** The CLI
layers a CWD `.env` and an explicit `--env-file` on top of the process
environment; nothing else reads configuration from disk.

Conventions to follow when you add one:

- **Prefix `NDIF_`.** Everything. Grouped by subsystem after that:
  `NDIF_QUEUE_*`, `NDIF_AUTOSCALING_*`, `NDIF_RAY_*`, `NDIF_OBJECT_STORE_*`,
  `NDIF_INFLUX_*`, `NDIF_LOKI_*`, `NDIF_POSTGRES_*`, `NDIF_DASHBOARD_*`.
- **Declare it where it's read**, in one place. For anything backed by an external
  service, that place is the provider's `CONFIG` dict — a
  `attr: (ENV_VAR, typed_default, cast)` tuple, which the base turns into
  `from_env()` / `to_env()` automatically (`common/providers/base.py:36`). For a
  subsystem, a module-level config object (`api/queue/config.py`). For a shell
  knob, a `${VAR:-default}` in the service's `start.sh` with the default documented
  in the header comment.
- **Empty means off.** The established idiom for an optional subsystem is that its
  URL defaults to `""` and an empty URL disables the whole thing:
  `NDIF_LOKI_URL`, `NDIF_POSTGRES_URL`, `NDIF_OBJECT_STORE_PUBLIC_URL`. Don't add a
  separate `_ENABLED` boolean unless you need to disable something that's otherwise
  configured (`NDIF_INFLUX_ENABLED` is the one such case).
- **Document the default in a comment next to the tuple**, and add a row to the
  README's `NDIF_*` table and [docs/reference/env-vars.md](../reference/env-vars.md).

Adding an env var that isn't in the README table is the most common drift in this
repo. Add the row.

## Logging and telemetry

- **One logger tree: `ndif`.** Every module does
  `logger = logging.getLogger("ndif.<component>")` — `ndif.api`, `ndif.request`,
  `ndif.queue.dispatcher`, `ndif.queue.replica`, `ndif.controller`,
  `ndif.modeling`. The sub-name becomes the Loki `logger` stream label, so it's a
  query dimension, not decoration. Never log to the root logger.
- **Use `event()`, not `logger.info(..., extra={...})`.**
  `common/telemetry.py:44` drops `None` fields and refuses keys that collide with
  reserved `LogRecord` attributes (which otherwise raises at runtime). It also sets
  `stacklevel=2` so the source context points at your call site.
- **Field names are a de-facto schema.** Grafana dashboards query them. Reuse the
  established ones — `model_key`, `replica_id`, `request_id`, `session_id`,
  `stage`, `status`, `duration_ms`, `queue_size`, `error_type` — rather than
  inventing a synonym.
- **Metrics are classes, not ad-hoc writes.** Add a `Metric` subclass in
  `common/metrics.py` with an explicit `update(...)` signature; that's where the
  tag/field split is decided. Tags must stay low-cardinality: no request ids, no
  replica ids, no IPs.
- **Telemetry never raises.** Both sinks fail open, in both senses — package
  missing and server unreachable. Preserve that.

## Where new code belongs

| If it is... | It goes in |
|---|---|
| Used by two or more services | `src/ndif/common/` |
| A connection to an external system | `src/ndif/common/providers/` + a pyproject extra |
| A Redis key/channel/stream name | `src/ndif/common/redis/` |
| On the request or response wire | `src/ndif/common/schema/` |
| Specific to one service | `src/ndif/services/<name>/` |
| A user-facing verb | `src/ndif/cli/commands/` (flags) + `src/ndif/cli/lib/` (logic) |

Two hard rules: `common/` never imports from `services/`, and CLI logic lives in
`lib/` — `commands/` holds click decoration only, because the dashboard backend
imports `lib/` directly and would otherwise have no door in.

Prefer subclassing an existing seam to adding a branch. The sandbox model actor is
the worked example: it is a handful of overridden hooks on
`BaseModelDeployment`'s `run` template (`modeling/base.py:244`), not a parallel
implementation. Same for providers (`Provider`) and model actors
(`BaseModelDeployment`).

## Typing and imports

`from __future__ import annotations` where the module uses modern syntax on
older-style annotations; `X | None` and `list[str]` in newer code, though the
older modules still carry `Optional`/`Dict`. Match the file you're in.

Heavy imports are deliberately deferred. `cli/lib/models.py:1` imports Ray and
nnsight *inside* functions so `ndif --help` stays fast, and `gunicorn_conf.py`
imports the telemetry providers only inside `post_fork` so the master never
connects them before forking. If you're tempted to hoist one of those to module
level, read the docstring first — it will tell you what breaks.

## Commits

Subjects are `area: lowercase imperative summary`, where `area` names the part of
the tree touched (`sandbox`, `api`, `queue`, `modeling`, `dashboard`, `cli`,
`docker`, `compose`, `telemetry`, `packaging`, `docs`, `tests`, `chore`). From the
log:

```
sandbox: let trusted requests skip the sandbox
queue: re-queue on eviction instead of erroring; lazy self-healing processor
api auth: a "trusted" key tag and a validate_request dependency
compose: default the sandbox model actor
docker: build nnsight's C extension so .save() works on the server
tests: remote cache and gradients now pass (flip from xfail)
```

The body explains the *why* and the constraint, at the same standard the source
comments hold to: name the failure mode, name the counterfactual. The best ones
here read like a small design note — see `git show f336ea6` for the shape.
Commits made with agent assistance carry a trailer:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

`main` is the branch; branch from it for your change.

## Before you open a PR

There is no automated gate, so this is on you:

1. Bring a stack up and run the live suite — `just up`, then `pytest tests/`. See
   [testing.md](./testing.md). An all-skipped run means the server wasn't up.
2. If you touched execution, run it a second time with `trusted=False` forced, so
   the sandbox path is covered too (testing.md has the recipe). Local dev is
   auth-off and therefore trusted-by-default; that path is otherwise never
   exercised.
3. `ruff check src/` if you have the `dev` extra. Nothing enforces it, but the
   tree is clean.
4. If you added an env var, update the README table and
   [docs/reference/env-vars.md](../reference/env-vars.md). If you added a `start.sh`
   or other non-`.py` file a service needs, update `[tool.setuptools.package-data]`
   in `pyproject.toml` — otherwise it works from a checkout and breaks from a wheel.

This is a young repo at version `0.0.1`. Nothing here is a stable public API, and a
change that deletes a concept is worth more than one that adds a flag.

## Related

- [testing.md](./testing.md) — the live suite and the untrusted-path recipe.
- [repo-layout.md](./repo-layout.md) — where things live, and the "change X, open Y" table.
- [adding-a-provider.md](./adding-a-provider.md) / [adding-a-service.md](./adding-a-service.md) — the two recipes that encode most of these conventions.
- [telemetry-internals.md](./telemetry-internals.md) — the logging and metrics plumbing behind the conventions above.
