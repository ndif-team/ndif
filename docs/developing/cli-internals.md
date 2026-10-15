---
title: CLI Internals
one_liner: How the `ndif` click app is assembled — command modules, env layering, PID-file state, model keys (with their task), and the lib/ layer the dashboard also calls — plus how to add a command.
tags: [internals, dev, cli]
related: [docs/operating/cli.md, docs/developing/dashboard-internals.md, docs/developing/queue-internals.md, docs/developing/controller-internals.md, docs/developing/redis-layer.md, docs/developing/repo-layout.md, docs/reference/env-vars.md, docs/developing/contributing.md]
sources: [src/ndif/cli/main.py, src/ndif/cli/config.py, src/ndif/cli/service.py, src/ndif/cli/state.py, src/ndif/cli/util.py, src/ndif/cli/lib/_common.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py, src/ndif/cli/lib/events.py, src/ndif/services/dashboard/backend/ndif_client.py]
---

# CLI Internals

## What this covers

The shape of `src/ndif/cli/` and the conventions a new command follows. The
user-facing reference is [CLI](../operating/cli.md). Two facts frame the design:

1. **Every knob is env-driven.** Services read their configuration from the
   environment (provider `CONFIG` specs and the `start.sh` scripts), so the CLI owns
   almost no configuration — it assembles an environment and hands it over.
2. **`lib/` is an API with two consumers.** `commands/` holds the verbs and all the
   click decoration; `lib/` holds the logic, and the dashboard backend imports it
   directly (`src/ndif/services/dashboard/backend/ndif_client.py`). A `lib/`
   signature change is a dashboard change.

| Module | Role |
|---|---|
| `main.py` | the click group, `--env-file`, command registration |
| `config.py` | `DEFAULTS`, `load_env_files`, `get`, `build_env` |
| `service.py` | the `Service` table: what can be started and how |
| `state.py` | `$NDIF_HOME` PID/log bookkeeping |
| `util.py` | logo, `pid_alive`, `terminate_pid`, `is_port_in_use` |
| `commands/*.py` | one click command per module — parsing, validation, rendering |
| `lib/*.py` | reusable implementations: deploy, scale, evict, restart, status, events, models, config files, connectivity checks |

## The group and command registration

`src/ndif/cli/main.py` is flat: one import per command module, then one loop —
`for _command in (start, stop, restart, deploy, scale, evict, status, queue, kill,
export, env, logs, info, doctor, version): cli.add_command(_command)`. No plugin
discovery, no lazy loading. The group callback runs `config.load_env_files` before
any subcommand, so `--env-file` must precede the verb:
`ndif --env-file prod.env deploy gpt2`.

**Import discipline.** Every command module is imported at startup, so they stay
cheap: `ray`, `nnsight`, `redis` and `yaml` are imported *inside* `lib/` functions,
so `ndif --help` and the service-lifecycle commands don't pull in the compute stack.
A top-level `import ray` in a command module slows every invocation.

## Config layering

`config.load_env_files` runs `load_dotenv(cwd/".env")`, then, if given,
`load_dotenv(env_file, override=True)`. The auto-discovered `./.env` never overrides
an exported variable; `--env-file` overrides everything.

`DEFAULTS` is a floor, never written into `os.environ`. Two accessors use it:

- `config.get(name)` — `os.environ.get(name, DEFAULTS.get(name))`; how `info`,
  `doctor`, `env` and `lib/` resolve `NDIF_RAY_ADDRESS` / `NDIF_REDIS_URL` /
  `NDIF_API_URL`.
- `config.build_env(env_pairs, typed)` — `{**DEFAULTS, **os.environ}`, then `-e
  KEY=VALUE` pairs, then the typed shortcuts in `ENV_OPTIONS`. This is the
  environment handed to a spawned service. A new typed shortcut is an `ENV_OPTIONS`
  entry plus the click option on `start`.

`DEFAULTS["NDIF_RAY_HEAD_PORT"]` is `6385`, matching `src/ndif/services/ray/start.sh`'s
own fallback, so the CLI, compose and a hand-run `start.sh` agree.

## Services and process state

`src/ndif/cli/service.py` is a table: a `Service` is a name, a `build_command(env)`,
a description, and an optional `build_env(env)`. `ray`, `api` and `dashboard` use
`_script_command` — their `start.sh` is the canonical launcher in and out of a
container, so the CLI passes no flags; `redis` and `minio` are external servers whose
commands are built from the matching `NDIF_*` URL. `SERVICES` is the ordered core
stack, `OPTIONAL_SERVICES` the opt-in ones, and `SERVICE_MAP` their union, which
`resolve_targets` and `ndif logs`'s `click.Choice` validate against. Adding a service
is [Adding a service](adding-a-service.md).

`src/ndif/cli/state.py` is the only persistence: `$NDIF_HOME/run/<name>.pid` and
`$NDIF_HOME/logs/<name>.log`. `State.running_pid` is the liveness primitive — read
the PID file, `kill(pid, 0)`, clear a stale file. `terminate_pid` signals the whole
**process group**, which is why `_spawn` passes `start_new_session=True`: a Ray head
or gunicorn master spawns children, and only a group signal takes them all down.

## The lib/ layer

Each `lib/` module exposes a function whose contract the dashboard depends on:

| Function | Returns | Side effects |
|---|---|---|
| `lib.deploy.deploy(specs, *, sync, ray_address, redis_url, on_message)` | `{"deployments": [...], "evictions": [...]}` | controller deploy, readiness wait, reconcile events |
| `lib.scale.scale(checkpoint \| model_key, *, n, task, gpus, ...)` | dict | adds replicas through the same deploy path |
| `lib.evict.evict(*, checkpoints \| model_keys \| evict_all, task, replica, ...)` | `{"results": [...]}` | controller evict, reconcile events |
| `lib.restart.restart(checkpoint \| model_key, *, task, replica, ...)` | `{"model_key", "replicas": [...]}` | `ray.kill` + readiness wait |
| `lib.status.status(*, ray_address)` | controller status dict | none |
| `lib.events.fetch_queue_state` / `kill_request` / `notify_reconcile` | dict / dict / `None` | Redis events stream |
| `lib.models.*` | below | none |

Three conventions make it work for both callers:

- **`on_message`** — an optional `Callable[[str], None]` progress callback. The CLI
  passes `click.echo`; the dashboard collects lines into `result["logs"]`. Emit
  through `_common.emit`, never `print`.
- **No click in `lib/`.** `lib/` raises `ValueError` for bad input and
  `NDIFConnectivityError` when Ray is unreachable; commands convert both to
  `click.ClickException` / `click.Abort`.
- **Structured returns.** JSON-serializable dicts the dashboard hands straight to
  FastAPI.

`src/ndif/cli/lib/_common.py` also owns `ensure_ray_connected` (liveness is
`RayProvider.connected()`, which requires the `Controller` actor) and
`normalize_specs`, the one place a deploy spec's shape is defined. Its fields:
`checkpoint`, `revision`, `task`, `pinned`, `replicas`, `actor_class`,
`envoy_class`, `trusted`, `execution_timeout_seconds`, the placement overrides
`padding_factor`, `padding_bias`, `size_bytes`, `gpus`, `max_tp`, and an optional
precomputed `model_key`. `dtype` is not normalized; `lib.deploy.deploy` carries it
across from the raw spec. Every field is reachable from `ndif deploy` flags
(`--task`, `--trusted`, `--dtype`, `--gpus`, `--size-bytes`, `--padding-factor`,
`--padding-bias`, `--max-tp`, `--actor-class`, ...) and from a `models.yaml` entry.

## Model keys and models.yaml

A model key is the canonical identity used everywhere downstream (controller,
dispatcher, actor names). `src/ndif/cli/lib/models.py` `get_model_key` builds the
nnsight wrapper class on meta — no weights — and calls `to_model_key()`:

```python
def get_model_key(checkpoint, revision=None, envoy_class=None, task=None) -> str:
    from nnsight.util import from_import_path
    cls = from_import_path(envoy_class or DEFAULT_ENVOY_CLASS)
    kwargs = {} if task is None else {"task": task}
    return cls(checkpoint, revision=revision, **kwargs).to_model_key()
```

`DEFAULT_ENVOY_CLASS` is `nnsight.modeling.transformers.TransformersModel`, and its
import path prefixes the key, so a key names the class to reconstruct, the repo, the
revision and the pipeline **task**. Left unset, the task is inferred from the
checkpoint exactly as an nnsight client with no `task=` infers it, so both mint the
same key; one checkpoint deployed under two tasks is two deployments. Helpers in the
same module:

- `extract_repo_id_from_model_key` — display-only string scan; returns the key
  unchanged when it can't find a repo id.
- `extract_task_from_model_key` — the `task` from the JSON suffix, or `None` for a
  suffix that carries none (a vLLM key, a wrapper with no task).
- `canonicalize_checkpoint(checkpoint, revision, envoy_class, task)` —
  `(repo_id, revision, model_key)` from one lookup, so the dashboard's schedule store
  persists both without resolving twice.

`src/ndif/cli/lib/model_config.py` is the whole `models.yaml` story. Its module
docstring is the file format. `load_model_config` accepts a `models:` list of strings
or mappings and reads every field the deploy path understands, filling the
rest from the caller's `default_*` values (which the CLI flags feed);
`build_models_list` is the single serializer behind `save_model_config` and
`ndif export --stdout`, collapsing to the bare-string form when an entry carries no
non-default options.

## The doctor checks

`src/ndif/cli/commands/doctor.py` has three checks that return a failure count and
one report that doesn't:

| Function | What it does |
|---|---|
| `_check_environment` | Python ≥ 3.12, then `ndif` and `nnsight` as hard requirements; `torch` (with its CUDA line), `transformers` and `ray` from `version.collect` — a missing one still counts |
| `_check_binaries` | `shutil.which` for `ray`, `redis-server`, `minio`, each with an install hint |
| `_check_gpu` | `nvidia-smi` lists GPUs and gives the driver's CUDA version; then `torch.cuda.is_available()` — a torch wheel built for a newer CUDA line than the driver imports fine and silently sees no GPU, so the mismatch is reported with both versions |
| `_report_connectivity` | `src/ndif/cli/lib/checks.py` probes; **never** counted as failures |

Connectivity returning `False` is a normal answer (the service is stopped), so
folding it into the exit code would make `ndif doctor` useless as a pre-flight check.
`lib/checks.py` functions keep that property: return a bool, never raise, never
mutate.

## Version reporting

- **`ndif --version`** is click's `version_option(package_name="ndif")`: the
  installed `ndif` version only.
- **`ndif version`** prints the stack: `python`, then `PACKAGES` (`ndif`, `nnsight`,
  `torch`, `transformers`, `ray`, `peft`, `accelerate`) from `importlib.metadata`,
  plus `cuda` from `torch.version.cuda`. A missing package renders `(not installed)`.
  `--json-output` emits the dict; `--write <path>` writes it to a file.
  `src/ndif/cli/commands/version.py` `collect` is shared with `doctor`, so the two
  never drift.

The image runs `ndif version --write /etc/ndif/build.json` *after* the dependency
layers (`docker/Dockerfile`), because the versions that decide the server's behaviour
are only known once pip has resolved them. Build-time inputs (`TORCH_CUDA`,
`TORCH_SPEC`, the git ref) go on OCI labels instead.

## Adding a command

1. **Create `src/ndif/cli/commands/<verb>.py`** with one `@click.command()` function
   named after the verb and a one-line module docstring `"""``ndif <verb>`` — purpose."""`.
2. **Put the logic in `lib/`** if it touches Ray, Redis or the controller, with an
   `on_message` callback. A CLI-only helper can stay private in the command module.
3. **Import Ray / nnsight / redis inside the function.**
4. **Register it** in `main.py`: the import and the `add_command` tuple.
5. **Follow the option conventions.** `--ray-address`, `--redis-url`, `--api-url`
   default to `NDIF_RAY_ADDRESS`, `NDIF_REDIS_URL`, `NDIF_API_URL` via `config.get`,
   documented as `(default: NDIF_*)`. Read-only commands take `--json-output`;
   anything worth polling takes `--watch` (2 s loop, `click.clear()`, clean return on
   `KeyboardInterrupt`). A command that resolves a checkpoint takes `--task`.
6. **Docstring with an `\b`-escaped `Examples:` block** — click renders it verbatim
   in `--help`.
7. **Errors the house way:** catch `NDIFConnectivityError` and `Exception`,
   `click.echo(f"✗ Error: {e}", err=True)`, `raise click.Abort()`; validation click
   types can't express raises `click.ClickException` before any work.
8. **Update [CLI](../operating/cli.md).**

A verb that manages a *process* rather than a model is a `Service`, not a command.

## Gotchas

- `commands/queue.py` renders a `status_changed_at` field that `Processor.snapshot()`
  never emits, so the "(for HH:MM:SS)" suffix never appears. Don't treat the CLI's
  render as a schema.
- `src/ndif/cli/lib/events.py` `_client` passes `socket_timeout=None` on purpose:
  redis-py 8+ otherwise applies a 5 s timeout against Redis 8, aborting the blocking
  wait before the dispatcher replies.
- `notify_reconcile` swallows every exception. A deploy that succeeded on the
  controller must not fail on a Redis hiccup — but a missed reconcile leaves a live
  Processor with a stale replica pool until its next refresh.
- `ndif info` iterates `SERVICES`, not `SERVICE_MAP`, so the dashboard never appears
  in its output even when a PID file tracks it.

## Testing

`tests/test_task_key_offline.py` covers the model-key and config plumbing with no
server; the rest of the CLI is exercised through the live-server suite. See
[Testing](testing.md).

## Related

[CLI](../operating/cli.md) for the commands themselves;
[Dashboard internals](dashboard-internals.md) for the second consumer of `lib/`;
[Controller internals](controller-internals.md) for the other side of deploy, evict
and status; [Queue internals](queue-internals.md) and [Redis layer](redis-layer.md)
for the events stream `queue`, `kill` and reconcile ride on.
