---
title: Testing
one_liner: The single live-server suite — how to bring a stack up and run it, what it covers, and how to force the untrusted sandbox path that local dev never exercises.
tags: [internals, dev, sandbox]
related: [docs/developing/contributing.md, docs/developing/repo-layout.md, docs/developing/sandbox-internals.md, docs/concepts/sandbox-execution.md, docs/concepts/auth-and-limits.md, docs/operating/quickstart.md, docs/developing/nnsight-integration.md, docs/runbooks/enable-auth.md]
sources: [tests/conftest.py, tests/test_nnsight_remote.py, tests/test_tensor_parallel_remote.py, tests/test_placement.py, pyproject.toml, justfile, docker/docker-compose.yml, src/ndif/services/api/auth.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/deployments/controller/controller.py]
---

# Testing

## What this covers

The honest current state: **there is no CI in this repo, and there is essentially
one test suite.** It lives in `tests/`, it drives the real `nnsight` client against
a real running NDIF over HTTP, and it skips itself entirely if nothing answers at
`http://localhost:8001`. "Bring the stack up, run pytest" is the whole story.

Three files, and only the first needs a server for every test:

| File | Needs | Covers |
|---|---|---|
| `test_nnsight_remote.py` | a stack + gpt2 | the remote trace surface, end to end |
| `test_tensor_parallel_remote.py` | a stack + a **deployed tensor-parallel replica** | a model split across GPUs — see the module docstring for the deploy |
| `test_placement.py` | nothing (skips without `boto3`) | placement arithmetic and shard-group bookkeeping, over synthetic objects |

`test_tensor_parallel_remote.py` skips itself unless a replica of its model is
actually HOT, so it costs nothing when you have not set one up. `test_placement.py`
is the odd one out — pure functions over a fake node, no server, no GPUs — and is
the only file here that will run in a checkout with nothing else going.

That shape is deliberate rather than accidental: almost everything NDIF does is a
cross-process, cross-container interaction — a serialized traced block leaving a
client, crossing Redis, crossing Ray, running against real weights, coming back as
a presigned blob. Mocking that proves nothing. So the suite tests the seam that
actually matters and accepts needing a GPU box to run.

## Running it

Install the test dependencies (the `dev` extra — `ruff`, `httpx`, `pytest`,
`pytest-asyncio`; `pyproject.toml:90`) plus a client `nnsight`:

```bash
pip install -e ".[dev]"
```

Bring a stack up and let it settle — the `ray` service needs a GPU and the NVIDIA
container toolkit, and it is slow to boot:

```bash
just up            # docker compose -f docker/docker-compose.yml up -d
just logs api ray  # watch until the api is serving and the controller is up
```

Then run the suite:

```bash
pytest tests/
```

You do **not** need to `ndif deploy` anything first. The suite uses
`openai-community/gpt2`, and the queue's `Processor` provisions a replica on
demand for any model key it hasn't seen; the first test pays the deploy time and
the module-scoped `model` fixture (`tests/conftest.py:60`) keeps it deployed for
the rest of the run.

> **Gotcha:** run `pytest tests/`, not a bare `pytest`. There is no
> `[tool.pytest.ini_options]` in `pyproject.toml` and no `testpaths`, so if a
> local `nnsight/` checkout is present, a bare invocation from the repo root also
> collects its `nnsight/tests/` (hundreds of client-side tests that have nothing
> to do with the server).

If the stack is down, collection still succeeds and every test is skipped —
`conftest.py:44` probes `GET /ping` once at import and builds a module-level
`requires_server` marker from the answer. A full skip therefore means "no server",
never "nothing to run".

Pointing at a non-default host means editing `HOST` in `tests/conftest.py:21`;
there is no environment variable for it.

## What the suite covers

51 tests across 18 classes in `tests/test_nnsight_remote.py`, each one opening a
real `with model.trace(..., remote=True):`.

| Class | What it proves about the server |
|---|---|
| `TestRemoteTrace` | Reading hidden states, logits, and module inputs over the wire. |
| `TestRemoteIntervention` | Writes land on the *real* forward — a zeroed early block changes a later block and the logits. |
| `TestRemoteEarlyStop` | `tracer.stop()` unwinds cleanly server-side. |
| `TestRemoteAdhocModule` | Calling a submodule out of execution order (logit lens). |
| `TestRemoteSource` | `.source` op-level reads, writes, and `skip()` inside a module's forward. |
| `TestRemoteAliasing` | `rename=` aliases resolve to the same remote module. |
| `TestRemoteSkip` | Skipping a whole block substitutes its output. |
| `TestRemoteSession` | A `model.session(remote=True)` runs several traces as one job with values flowing between them. |
| `TestRemoteGenerate` | Multi-token generation, `tracer.iter[:3]`, the streamer, `tracer.result`. |
| `TestRemoteGradients` | Backward works once an activation is opted in — the server loads weights with `requires_grad_(False)`. |
| `TestRemoteCache` | `tracer.cache()` round-trips and captures every reached module. |
| `TestRemotePeft` | A per-request PEFT adapter id rides on the request env and is applied before the run. |
| `TestNdif` | `nnsight.status()`, `is_model_running`, `get_remote_env`, `compare` against the live `/status` and `/env`; also `test_persistent_objects`, which tokenizes inside the block and asserts it matches a local tokenization — on the sandbox path that only passes because the runner resolves `Tokenizer` from its own meta model. |
| `TestRemoteLocalCode` | Non-installed local modules/classes ship to the server by value automatically. |
| `TestRemoteNonBlocking` | `blocking=False` submit, then poll `GET /response/{id}` — needs object-store response persistence. |
| `TestRemoteEdit` | `model.edit()` edits ride to the server and apply. |
| `TestRemoteBatching` | Several `tracer.invoke(...)` blocks in one forward: batched results match solo runs and edits stay scoped to their rows. |
| `TestRemoteSaving` | Both `nnsight.save(v)` and `v.save()`, including values computed inside the block. |

And in `test_tensor_parallel_remote.py`, against a replica whose weights are split
across GPUs by `TPModelActor`. None of these traces mention sharding — that is the
point; they assert the two things a broken shard-and-gather gets wrong, the
**width** of a value and whether the answer matches an unsharded run:

| Class | What it proves about the server |
|---|---|
| `TestShardedWidths` | A value arrives whole — neither a fraction of the real tensor nor, on a tied LM head, a multiple of it. |
| `TestShardedIntervention` | An edit to a sharded value reaches the model, including one straddling a rank boundary. |
| `TestShardedDeterminism` | Every rank runs the block, so the replica answers consistently and generation reproduces. |
| `TestShardedFailure` | A block that raises costs the request, not the replica — every rank raised it, which is a settled group. |
| `TestShardedBatching` | Several invokes narrow the same gathered tensor identically on every rank, so no invoke answers another's prompt. |

Two classes carry their own guards rather than the server marker: `TestRemotePeft`
skips unless the *client* has `peft` (it needs it to graft the adapter
architecture onto the meta model), and `TestRemoteLocalCode` resets
`nnsight.ndif._PULLED_ENV` per test so `pull_env` re-scans and picks up the module
it just wrote.

## The xfail convention

The suite's convention for a limitation of the *server* — as opposed to a client
bug — is a class-level

```python
@pytest.mark.xfail(reason="<what the server can't do yet>", strict=False)
```

`strict=False` is the load-bearing part. It means the test still executes the real
remote path, a failure is reported as an expected `XFAIL` and doesn't break the
run, and — the point — the day the server gains support, pytest reports an
**XPASS**. An XPASS is a notification: a documented limitation is gone, and the
xfail marker plus its `reason` should be deleted in the same change that made it
pass. That is how gradients and `tracer.cache()` were retired (`git show
c6a0292`), each flip carrying the explanation into an inline comment.

**There are currently no xfail classes** — the suite is fully green. The module
docstring at `tests/test_nnsight_remote.py:8` still refers to "the `xfail` classes
at the bottom"; that text is stale. Add one when you find a genuine server-side
limitation, rather than deleting or `skip`ping the test.

## Testing the untrusted / sandbox path

This is the part local dev gets wrong by default, and it is worth being explicit
about, because **a normal `just up` never runs a single line of sandbox code.**

Two independent things have to be true for a request's Python to run in a runner
process rather than in the model actor:

1. **The request must be untrusted.** The fork is `if request.trusted:` in
   `SandboxModelDeployment.execute` (`src/ndif/services/ray/sandbox/model.py:242`),
   which defers straight to the base in-process implementation. Under auth-off
   (`NDIF_POSTGRES_URL` unset, which the dev compose leaves commented out) a
   request's `trusted` **defaults** to `True`, so a plain local request is trusted —
   but the client can now override that. `validate_request` honors an explicitly
   supplied `trusted` when auth is off (it checks `model_fields_set` to tell
   "unspecified" from an explicit value, `src/ndif/services/api/auth.py:170,184`),
   so **sending `trusted: false` in the request forces the sandbox path with no
   Postgres and no code patch.**
2. **The deployed actor class must be the sandbox one.** The controller's code
   default is the plain in-process actor —
   `ndif.services.ray.deployments.modeling.base.ModelActor`
   (`controller.py:558`). The dev compose selects the sandbox actor via
   `NDIF_DEFAULT_MODEL_ACTOR_CLASS: ndif.services.ray.sandbox.model.SandboxModelActor`
   (`docker/docker-compose.yml:228`, the fallback for `NDIF_MODEL_IMPORT_PATH`), but
   a hand-rolled `ndif start ray` that sets neither gets the base actor, where
   `trusted` is irrelevant because there is no sandbox to skip.

So: with compose you already have condition 2. Condition 1 is a change to the
**request**, not the server — but note that nnsight's `RequestModel` has no
`trusted` field (the server reads it from the request body, where an API key
would normally stamp it), so a client cannot set it through the nnsight API. It
has to be injected into the envelope. `tests/conftest_untrusted.py` does exactly
that, in about ten lines:

```bash
PYTHONPATH=tests pytest tests/test_nnsight_remote.py -p conftest_untrusted
```

Every test must produce the same result as the trusted run, and as of the
autocast fix below they do — bar `TestRemoteGradients`, which is a real sandbox
limitation (see `sandbox/ARCHITECTURE.md`). **46 passed, 2 failed, 2 skipped**
through runner processes.

Worth knowing what "the same result" turned out to mean. The two paths were not
observationally identical, in two compounding ways, and neither failed anything —
the numbers were just different:

* the runner had no autocast region, so a tensor the *block* made came back
  `float32` untrusted and `bfloat16` in-process;
* and the host had none around the forward it drives on the runner's behalf, so
  the *model's own* arithmetic ran uncast. Measured on gpt2: identical token ids
  and identical embeddings, diverging inside the first transformer block, ending
  at a relative difference of 6.5e-3 in the logits.

Both now use one `request_dtype` bracket and the paths are bit-identical. If you
are checking this yourself, run the same path twice first — trusted against
trusted is bit-exact, which is what makes a trusted-vs-untrusted difference mean
anything.

That invariant — the two paths are observationally identical — is the whole
reason the sandbox is shaped the way it is, so the suite doubles as the sandbox's
conformance test. Any divergence is a sandbox bug (or a new xfail), and
`src/ndif/services/ray/sandbox/ARCHITECTURE.md`'s "Current simplifications"
section is where the deliberate ones are listed. Read that list against the code
before believing it: `tracer.cache()` is listed as unsupported but is in fact
served over IPC now (`sandbox/model.py:133`, `sandbox/nns.py:316`).

**The faithful way**, if you want the real trust plumbing rather than a client-set
flag: uncomment `NDIF_POSTGRES_URL` in `docker/docker-compose.yml:156`, insert a
user and a key into the compose Postgres (`docker/postgres/init.sql` creates the
schema but seeds nothing), and *don't* grant that key the `trusted` user_tag. Then
set `NDIF_API_KEY` for the client. With auth on, the key's `trusted` user_tag
decides and a client-supplied `trusted` is overwritten; this also exercises the
401/400/403/503 ladder in `verify_api_key`, which nothing else does.

Confirm which path a request actually took by watching the ray service —
a sandboxed request spawns a runner subprocess per request:

```bash
just logs ray
docker compose -f docker/docker-compose.yml exec ray \
    pgrep -af 'ndif.services.ray.sandbox.runner'
```

## Gotchas

- **A skipped suite is not a passing suite.** `pytest tests/` with no server
  reports all-skipped and exits 0.
- **Ray is slow to boot and needs a GPU.** `just up` returns long before the
  controller is serving; the first test will otherwise fail on deploy.
- **The client's nnsight must match the server's.** These are live end-to-end
  tests of a serialization contract; a client/server nnsight mismatch shows up as
  a deserialization error, not a version message unless `NDIF_MIN_NNSIGHT_VERSION`
  is set. See [nnsight-integration.md](./nnsight-integration.md).
- **`shm_size`.** Ray's plasma store lives in `/dev/shm`; compose sets `4gb`
  (`docker/docker-compose.yml:256`) because the Docker default of 64MB is far too
  small.
- **No unit tests exist** for the queue, the controller's placement math, or the
  providers. Changing those means either exercising them through the live suite or
  writing the first unit test for them — both are welcome, neither is set up.

## Related

- [contributing.md](./contributing.md) — house style and what to run before a PR.
- [sandbox-internals.md](./sandbox-internals.md) — what you're actually exercising when you force `trusted=False`.
- [docs/concepts/auth-and-limits.md](../concepts/auth-and-limits.md) — why auth-off implies trusted.
- [docs/runbooks/enable-auth.md](../runbooks/enable-auth.md) — the full Postgres-auth setup.
- [docs/operating/quickstart.md](../operating/quickstart.md) — getting a stack up in the first place.
