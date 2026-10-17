---
title: Contributing
one_liner: House style and conventions this codebase follows — comments, docstrings, env vars, logging, where code belongs, commit form — and what CI does and does not do.
tags: [internals, dev]
related: [docs/developing/testing.md, docs/developing/repo-layout.md, docs/developing/providers.md, docs/developing/adding-a-service.md, docs/developing/telemetry-internals.md, docs/reference/env-vars.md]
sources: [src/ndif/common/providers/base.py, src/ndif/common/providers/loki.py, src/ndif/common/telemetry.py, src/ndif/common/redis/__init__.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/sandbox/ARCHITECTURE.md, pyproject.toml, .github/workflows/build_images.yml, .github/workflows/publish_docker.yml, .github/workflows/publish.yml]
---

# Contributing

## What this covers

The root [CONTRIBUTING.md](../../CONTRIBUTING.md) is the entry point: branches,
running the tests, the docs standard, bug reports. This page is the dev-facing depth
behind it — the house style visible in the source, stated so you can hold the bar
without reverse-engineering it — and what CI does. Where the code and a comment
disagree, the code wins and the comment is a bug.

## Comments and docstrings

**The module docstring is where the concept gets taught.** A module docstring here
states the design constraint, then the mechanism. Compare
`src/ndif/common/providers/loki.py`, which says why Loki shipping is fail-open and
lazy before it says what the handler does, or `src/ndif/common/metrics.py`, which
teaches InfluxDB's tags-vs-fields split and the cardinality reasoning behind every
choice in the file. A new module in `common/` or a new subsystem without a teaching
docstring is incomplete.

Below that, three layers:

- **Module docstring** — the concept, the constraint, the shape.
- **Class/function docstring** — the contract and the reasoning.
  `src/ndif/common/providers/redis.py` `RedisProvider.connect` spends its docstring
  on why `socket_timeout=None` is passed explicitly, because someone would
  otherwise delete it.
- **Inline comment** — why *this* implementation, never what the line does.

**Comment the why, and name the failure mode.** The comments that earn their place
say what breaks without the line: "without this they'd default to localhost:6379 —
and inside this container nothing listens there", "a thread created in the master
would be dead in every forked worker", "a silent 'auth off' would be a security
hole". Comments that restate the line get deleted.

**Describe the code, not its history.** A comment says what the code does and why;
the before/after of a change belongs in the commit message.

**Big subsystems get a design doc.** `src/ndif/services/ray/sandbox/ARCHITECTURE.md`
is the model: the interleaving split, a table of files, a diagram, and an explicit
"current simplifications" section. If you build something with that much internal
structure, write one next to the code and point the docstrings at it.

## Configuration and env vars

**Every knob is an environment variable; there is no config file.** The CLI layers a
CWD `.env` and an explicit `--env-file` over the process environment; nothing else
reads configuration from disk. When you add one:

- **Prefix `NDIF_`**, then group by subsystem: `NDIF_QUEUE_*`, `NDIF_AUTOSCALING_*`,
  `NDIF_RAY_*`, `NDIF_OBJECT_STORE_*`, `NDIF_INFLUX_*`, `NDIF_LOKI_*`,
  `NDIF_POSTGRES_*`, `NDIF_DASHBOARD_*`.
- **Declare it where it's read**, once. For an external service, the provider's
  `CONFIG` dict — `attr: (ENV_VAR, typed_default, cast)`, which
  `src/ndif/common/providers/base.py` `Provider` turns into `from_env()` /
  `to_env()`. For a subsystem, a module-level config object
  (`src/ndif/services/api/queue/config.py`). For a shell knob, `${VAR:-default}` in
  the service's `start.sh` with the default in the header comment.
- **Empty means off.** An optional subsystem's URL defaults to `""` and empty
  disables it (`NDIF_LOKI_URL`, `NDIF_POSTGRES_URL`). Add an `_ENABLED` boolean only
  to disable something otherwise configured (`NDIF_INFLUX_ENABLED`).
- **Document the default** next to the tuple, and add a row to
  [Environment variables](../reference/env-vars.md) — the most common drift in this
  repo.

## Logging and telemetry

- **One logger tree: `ndif`.** Every module takes
  `logging.getLogger("ndif.<component>")`; the sub-name becomes the Loki `logger`
  label, so it is a query dimension. Never log to the root logger.
- **Use `event()`**, not `logger.info(..., extra={...})`: it drops `None` fields,
  refuses keys that collide with `LogRecord` attributes, and sets `stacklevel=2`.
- **Field names are a de-facto schema** that Grafana queries — reuse `model_key`,
  `replica_id`, `request_id`, `session_id`, `stage`, `status`, `duration_ms`,
  `queue_size`, `error_type` rather than inventing synonyms.
- **Metrics are classes**, not ad-hoc writes: a `Metric` subclass in
  `src/ndif/common/metrics.py` decides the tag/field split. Tags stay
  low-cardinality — no request ids, replica ids or IPs.
- **Telemetry never raises.** Both sinks fail open; preserve that.

The mechanics are [Telemetry internals](telemetry-internals.md).

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
`lib/` — the dashboard backend imports `lib/` directly.

Prefer subclassing an existing seam to adding a branch. The sandbox model actor is a
handful of overridden seams on `BaseModelDeployment.run`, not a parallel
implementation ([Adding a model actor](adding-a-model-actor.md)); the same goes for
providers (`Provider`).

## Typing and imports

`X | None` and `list[str]` in newer code; older modules still carry
`Optional`/`Dict`. Match the file you're in.

Heavy imports are deferred on purpose. `src/ndif/cli/lib/models.py` imports Ray and
nnsight inside functions so `ndif --help` stays fast, and
`src/ndif/services/api/gunicorn_conf.py` imports the telemetry providers only in
`post_fork` so the master never connects them before forking. Before hoisting one to
module level, read the docstring — it says what breaks.

## Commits

Subjects are `area: lowercase imperative summary`, where `area` names the part of the
tree (`sandbox`, `api`, `queue`, `modeling`, `dashboard`, `cli`, `docker`, `compose`,
`telemetry`, `packaging`, `docs`, `tests`, `chore`):

```
sandbox: let trusted requests skip the sandbox
queue: re-queue on eviction instead of erroring; lazy self-healing processor
api auth: a "trusted" key tag and a validate_request dependency
compose: default the sandbox model actor
docker: build nnsight's C extension so .save() works on the server
```

The body explains the *why* and the constraint, at the standard the source comments
hold: name the failure mode and the counterfactual.

## CI and releases

The workflows in `.github/workflows/` build and publish; none runs the tests or
`ruff`.

| Workflow | Trigger | Does |
|---|---|---|
| `build_images.yml` | manual only | builds the hosted image into ECR for `dev`, `test` or `prod`; with `deploy` ticked, also force-redeploys the API and Dashboard services |
| `publish_docker.yml` | a published GitHub release (or manual) | publishes the `ndif/ndif` images to Docker Hub; `latest` moves only for a final `X.Y.Z` |
| `publish.yml` | a published GitHub release | builds and publishes the wheel to PyPI |

One GitHub release fires both publish workflows; a release tag is a release, not
a checkpoint. The package version is the git tag
(setuptools-scm); the image build has no `.git`, so the version reaches it through
the `NDIF_VERSION` build arg.

## Before you open a PR

The root [CONTRIBUTING.md](../../CONTRIBUTING.md#tests) has the test commands; beyond
those:

1. If you touched execution, also run the live suite with `trusted=False` forced
   ([Testing](testing.md#testing-the-untrusted--sandbox-path)) — local dev is
   trusted by default, so the sandbox path is otherwise never exercised.
2. `ruff check src/` with the `dev` extra; nothing enforces it.
3. A new env var gets a row in [Environment variables](../reference/env-vars.md); a
   new `start.sh` or other non-`.py` file gets a `package-data` entry
   ([Adding a service](adding-a-service.md#dependencies-extras-and-the-image)).
4. After a code change run `just ta`. `just ta <svc>` tears the **whole** stack down and brings back only that service and its dependencies; HOT deployments are lost.
5. A doc your change makes false is fixed in the same PR; `pytest tests/test_docs.py`
   checks cited paths, symbols and links.

Nothing here is a stable public API, and a change that deletes a concept is worth
more than one that adds a flag.

## Related

[Testing](testing.md) for the suite and the untrusted-path recipe;
[Repo layout](repo-layout.md) for where things live; [Providers](providers.md) and
[Adding a service](adding-a-service.md) for the two recipes that encode most of
these conventions.
