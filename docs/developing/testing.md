---
title: Testing
one_liner: The test suite — which files need a live server and which run anywhere, how to bring a stack up and run it, what each file covers, and how to force the untrusted sandbox path that local dev never exercises.
tags: [internals, dev, sandbox]
related: [docs/developing/contributing.md, docs/developing/repo-layout.md, docs/developing/sandbox-internals.md, docs/concepts/sandbox-execution.md, docs/concepts/auth-and-limits.md, docs/operating/quickstart.md, docs/developing/nnsight-integration.md, docs/runbooks/enable-auth.md]
sources: [tests/conftest.py, tests/conftest_untrusted.py, tests/test_nnsight_remote.py, tests/test_tensor_parallel_remote.py, tests/test_placement.py, pyproject.toml, justfile, docker/docker-compose.yml, src/ndif/services/api/auth.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/deployments/controller/controller.py]
---

# Testing

## What this covers

Everything lives in `tests/`. **No workflow runs it** — the workflows build and
publish ([Contributing](contributing.md#ci-and-releases)) — so a change is exercised
when someone runs the suite. The files fall into three groups.

**Live server** — drive the real nnsight client against a running NDIF over HTTP,
and skip themselves if nothing answers at `http://localhost:8001`:

| File | Needs | Covers |
|---|---|---|
| `test_nnsight_remote.py` | a stack + gpt2 | the remote trace surface end to end ([below](#the-live-suite)) |
| `test_sandbox_conformance.py` | a stack + gpt2 | trusted and untrusted run the same script and are compared |
| `test_tensor_parallel_remote.py` | a stack + a deployed tensor-parallel replica | a model split across GPUs; skips unless that replica is HOT (the module docstring has the deploy) |

**Model, no server** — a real gpt2 on CPU, a spawned runner and a `SandboxDriver`
over the socket, compared against the local run; no Ray:

| File | Covers |
|---|---|
| `test_sandbox_iter_pins.py` | the `tracer.iter` pin surviving control events |
| `test_sandbox_barriers.py` | `tracer.barrier()` across the socket: cross-invoke releases, reuse, and an unsatisfiable count raising the trusted path's `ValueError` |
| `test_sandbox_error_masking.py` | errors crossing the sandbox as themselves (`OutOfOrderError`, the host's own exception — never `BrokenPipeError`), and telemetry's `error_type` naming the real type on both paths (the `report`-wiring tests also need `ray` importable) |
| `test_sandbox_recursive_source.py` | recursive `.source` over the socket, against the local run |
| `test_sandbox_adhoc_calls.py` | an ad-hoc module call (`hook=False` and `hook=True`) leaving the module's real occurrence alone |

**Offline** — no model, no server, no GPU:

| File | Covers |
|---|---|
| `test_placement.py` | placement arithmetic, eviction bookkeeping and placement overrides over synthetic nodes, plus process-free checks of the tensor-parallel and sandbox actor plumbing (seeding, runner env, error reporting, the opt-in switch) |
| `test_fanout.py` | the `Fanout` barrier that lets one runner drive several ranks |
| `test_replica_wait.py` | what counts as "not yet" versus "never" when waiting on a replica |
| `test_node_registry.py` | the controller noticing nodes that are gone (a stale registry after mass node loss) and `summarize_error` producing a useful line |
| `test_inline_result_cap.py` | `RedisProvider.max_publish_bytes`: the derivation from Redis's pubsub limits and every fallback |
| `test_result_serialization.py` | the result pickler: client classes round-trip as themselves, block-born classes as working twins, CUDA tensors to CPU |
| `test_task_key_offline.py` | the pipeline task's place in the model key, config files and spec normalization |
| `test_utils.py` | the per-request report helpers in `src/ndif/services/ray/deployments/modeling/util.py` (`request_meta`, OOM shortfall) |
| `test_docs.py` | the docs gate: cited paths and symbols exist, links and anchors resolve, line-number citations don't grow |

The live files are the ones that test the seam that matters most — a serialized
block leaving a client, crossing Redis and Ray, running against real weights and
coming back — which no mock reproduces; the offline ones pin the parts that are
pure arithmetic or pure protocol.

## Running it

Install the `dev` extra (`ruff`, `httpx`, `pytest`, `pytest-asyncio`) plus a client
nnsight:

```bash
python -m venv --system-site-packages .venv && . .venv/bin/activate   # optional: keeps the dev extra out of a shared env
pip install -e ".[dev]"
```

The offline and model-only files run with nothing else: `pytest tests/test_placement.py`.
For the live files, bring a stack up and let it settle — the `ray` service needs a
GPU and the NVIDIA container toolkit, and it is slow to boot:

```bash
just up            # docker compose -f docker/docker-compose.yml up -d
just logs api ray  # watch until the api is serving and the controller is up
pytest tests/
```

You don't need to `ndif deploy` first: the suite uses `openai-community/gpt2`, the
queue provisions a replica on demand for a model key it hasn't seen, and the
module-scoped `model` fixture in `tests/conftest.py` keeps it deployed for the run —
only the first test pays the deploy time.

With no stack, collection still succeeds and every live test is skipped:
`tests/conftest.py` probes `GET /ping` once at import and builds the
`requires_server` marker from the answer. Pointing at a non-default host or port
means editing `HOST` in `tests/conftest.py`; there is no environment variable for
it. Through an SSH tunnel, use the tunnel's local port.

One client-side requirement: the non-blocking tests
download from `NDIF_OBJECT_STORE_PUBLIC_URL`, which must be reachable from the
machine running pytest
([Quickstart](../operating/quickstart.md#client-on-another-machine)).

> **Gotcha:** run `pytest tests/`, not a bare `pytest`. `pyproject.toml` has no
> `[tool.pytest.ini_options]` or `testpaths`, so a bare invocation from the repo
> root collects whatever else is under it — such as an nnsight checkout parked there
> for development.

## The live suite

`tests/test_nnsight_remote.py`: 65 tests in 22 classes, each opening a real
`remote=True` trace or session.

| Class | What it exercises on the server |
|---|---|
| `TestRemoteTrace` | reading hidden states, logits and module inputs over the wire |
| `TestRemoteIntervention` | writes landing on the real forward — a zeroed early block changes later blocks and the logits |
| `TestRemoteEarlyStop` | `tracer.stop()` unwinding server-side |
| `TestRemoteAdhocModule` | calling a submodule out of execution order (logit lens) |
| `TestRemoteSource` | `.source` op-level reads, writes and `skip()` |
| `TestRemoteAliasing` | `rename=` aliases resolving to the same module |
| `TestRemoteSkip` | skipping a whole block |
| `TestRemoteSession` | a `model.session(remote=True)` running several traces as one job |
| `TestRemoteGenerate` | multi-token generation, `tracer.iter[:3]`, the streamer, `tracer.result` |
| `TestRemoteGradients` | backward through an activation opted into the graph (weights load with `requires_grad_(False)`) |
| `TestRemoteCache` | `tracer.cache()` round-tripping |
| `TestRemotePeft` | a per-request PEFT adapter applied before the run (skips unless the client has `peft`) |
| `TestNdif` | `nnsight.status()`, `is_model_running`, `get_remote_env`, `compare`; and tokenizing inside the block, which on the sandbox path depends on the runner's meta-model tokenizer |
| `TestRemoteLocalCode` | non-installed local modules shipping by value (resets `nnsight.ndif._PULLED_ENV` per test) |
| `TestRemoteNonBlocking` | `blocking=False` submit, then `GET /response/{id}` from the object store |
| `TestRemoteEdit` | `model.edit()` edits applied server-side |
| `TestRemoteBatching` | several invokes in one forward: results match solo runs, edits stay in their rows |
| `TestRemoteBarrier` | `tracer.barrier(n)` sequencing invokes |
| `TestRemoteSaving` | `nnsight.save(v)` and `v.save()`, including values computed in the block |
| `TestRemoteMeta` | the job-cost report (`meta`) the actor attaches to `COMPLETED` |
| `TestRemoteAsync` | the async backend (`await`, `async for`) against a live job |
| `TestRemoteOOM` | an allocation over the job's GPU budget failing as a bounded, non-fatal error |

`tests/test_tensor_parallel_remote.py` runs against a replica sharded by
`TPModelActor`. Its traces never mention sharding; they check what a broken
shard-and-gather gets wrong — a value's **width** and agreement with an unsharded
run — in `TestShardedWidths`, `TestShardedIntervention`, `TestShardedDeterminism`,
`TestShardedFailure` and `TestShardedBatching`.

## Testing the untrusted / sandbox path

**A normal `just up` never runs sandbox code.** Two things must hold for a block to
run in a runner process:

1. **The request is untrusted.** `SandboxModelDeployment.execute`
   (`src/ndif/services/ray/sandbox/model.py`) sends a trusted request to the base
   in-process path. With auth off unspecified `tags` default to `{"trusted"}`, but
   `validate_request` (`src/ndif/services/api/auth.py`) honors explicit ones —
   so sending `tags: []` forces the sandbox path with no Postgres.
2. **The deployed actor class is the sandbox one.** The controller's default is the
   in-process `ModelActor`; the compose stack selects `SandboxModelActor` through
   `NDIF_DEFAULT_MODEL_ACTOR_CLASS`. A hand-run `ndif start ray` that sets neither
   has no sandbox to use.

With compose, condition 2 holds. nnsight's request envelope has no `tags` field,
so condition 1 means injecting it: `tests/conftest_untrusted.py` patches
`RequestModel.metadata` to add `"tags": []`, as a plugin that forces the
whole run untrusted:

```bash
PYTHONPATH=tests pytest tests/test_nnsight_remote.py -p conftest_untrusted
```

Compare against the trusted run. The untrusted path's known limits — gradients,
weight access, training — are listed in
[Sandbox internals](sandbox-internals.md#what-the-untrusted-path-cannot-do), so
`TestRemoteGradients` is expected to fail there.

`tests/test_sandbox_conformance.py` turns the comparison into an assertion: it runs
one script both ways against a live server and compares the results. It refuses to
run under `-p conftest_untrusted`, which patches the same method and would compare
untrusted against untrusted. The comparison holds because both paths enter the
same `request_dtype` autocast region — the runner around the block, the
`SandboxDriver` around the forward it drives.

**With real auth** instead of a client-set flag: enable Postgres
([Enable auth](../runbooks/enable-auth.md)), create a key *without* the `trusted`
tag, and set `NDIF_API_KEY` for the client. The key's tags then decide and
client-supplied `tags` are overwritten; this also exercises `verify_api_key`'s
401/400/403/503 responses, which nothing else does.

To confirm which path a request took, look for a runner process per request:

```bash
docker compose -f docker/docker-compose.yml exec ray \
    pgrep -af 'ndif.services.ray.sandbox.runner'
```

## Gotchas

- **A skipped suite is not a passing suite.** `pytest tests/` with no server skips
  every live test and exits 0.
- **Ray is slow to boot.** `just up` returns long before the controller serves; a
  run started too early fails on the first deploy.
- **The client's nnsight must match the server's.** A mismatch shows up as a
  deserialization error, not a version message, unless `NDIF_MIN_NNSIGHT_VERSION` is
  set — see [nnsight integration](nnsight-integration.md#when-client-and-server-versions-differ).
- **`shm_size`.** Ray's object store lives in `/dev/shm`; compose sets `4gb` on the
  `ray` service because Docker's 64 MB default is far too small.
- **Coverage is uneven.** The dispatcher, the API app and the providers other than
  Redis's publish cap are exercised only through the live suite.

Changing behavior that a doc describes? Update the doc in the same PR — the docs
standard is in the root [CONTRIBUTING.md](../../CONTRIBUTING.md#docs).

## Related

[Contributing](contributing.md) for house style; [Sandbox internals](sandbox-internals.md)
for what an untrusted request exercises; [Auth and limits](../concepts/auth-and-limits.md)
for why auth-off implies trusted; [Quickstart](../operating/quickstart.md) for
bringing a stack up.
