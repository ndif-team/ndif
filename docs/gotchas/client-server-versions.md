---
title: Client and Server Version Coupling
one_liner: How tightly an NDIF server is bound to the nnsight it was built against — the version gate and what a rejected client sees, the Status enum being the client's enum, source-not-bytecode serialization, and the client-side packages a user needs to match a server-side model.
tags: [gotchas, dev, api, auth, errors]
related: [docs/developing/nnsight-integration.md, docs/concepts/auth-and-limits.md, docs/concepts/status-and-results.md, docs/reference/schemas.md, docs/reference/http-api.md, docs/errors/client-side-failures.md, docs/operating/models-and-deployment.md, docs/reference/external-resources.md]
sources: [src/ndif/services/api/versioning.py, src/ndif/services/api/app.py, src/ndif/common/schema/response.py, src/ndif/common/schema/request.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/nns.py, pyproject.toml, requirements.txt, docker/Dockerfile]
---

# Client and Server Version Coupling

## What this covers

NDIF is not a language-agnostic HTTP service that happens to be used by nnsight.
It **imports nnsight** and re-exports nnsight's own types as its wire format. Two
facts follow, and they frame everything below:

1. **The protocol is nnsight's Python objects.** `BackendRequestModel` subclasses
   nnsight's `RequestModel`; `BackendResponseModel` subclasses nnsight's
   `ResponseModel` and `Status` *is* nnsight's `Status`
   (`src/ndif/common/schema/response.py:1`). There is no separate schema to
   version independently — the client and server agree because they are running
   the same class definition.
2. **The user's traced block ships as source, not bytecode.** nnsight's
   serialization module reduces the block to `(source, globals, locals)` and the
   server recompiles it. That buys real tolerance across Python versions, and
   none at all across library versions.

The practical shape: **Python-version drift is fine, nnsight-version drift is
not, and third-party-library drift breaks in whatever way that library's pickles
break.**

## The version gate

The nnsight client stamps two headers on every `POST /request`:
`nnsight-version` (from its installed distribution metadata) and `python-version`
(the full `sys.version` string). `validate_client_versions`
(`src/ndif/services/api/versioning.py:77`) runs before any other work in the
handler (`src/ndif/services/api/app.py:140`).

| Variable | Example | Unset / empty |
|---|---|---|
| `NDIF_MIN_NNSIGHT_VERSION` | `0.5.0` | no nnsight gating at all |
| `NDIF_MIN_PYTHON_VERSION` | `3.10` | no python gating at all |

Both default to unset, so **an out-of-the-box NDIF gates nothing** — an ancient
client gets through and fails later, deep in deserialization, with a message
about pickles rather than about versions. Setting `NDIF_MIN_NNSIGHT_VERSION` is
the cheapest operational improvement available to a self-hoster.

Three things produce a `400` once a minimum is set: a **missing** header (read as
"outdated nnsight"), an **unparseable** version, and a version **below** the
minimum. The python check compares major.minor only, dropping patch and build
noise (`versioning.py:64`).

### What a rejected client actually sees

Not a bare status line. The client's `_post` unpacks FastAPI's `{"detail": ...}`
body and raises `RemoteError` with the sentence, so the user gets:

```
RemoteError: Client nnsight version 0.4.1 is below the minimum supported 0.5.0.
Please `pip install --upgrade nnsight`.
```

The same holds for every auth failure (401/403/503) and for the 503 raised when
Ray is disconnected. If a user reports a bare HTTP code with no message, they are
not going through nnsight's remote backend.

> **Gotcha:** both variables are read **once at import**
> (`versioning.py:23-24`), so a change only takes effect on a fresh API process.
> Editing the compose `environment:` block means `just up api` (which recreates
> the container); `just restart api` bounces the process without re-reading the
> file. And `or None` means an **empty string counts as unset**:
> `NDIF_MIN_NNSIGHT_VERSION=""` silently disables the gate rather than rejecting
> everyone.

> **Gotcha:** an nnsight installed from a source tree rather than as a
> distribution reports an **empty** version string. With a minimum configured
> that is a 400 ("Client nnsight version was not provided"), which is confusing
> for a developer running nnsight from a checkout. `pip install -e` the checkout
> so the distribution metadata exists.

## The `Status` enum is the client's enum

`Status` is imported from nnsight and re-exported unchanged. Every status the
server publishes — `RECEIVED`, `QUEUED`, `PROVISIONING`, `DEPLOYING`,
`DISPATCHED`, `RUNNING`, `COMPLETED`, `ERROR`, `LOG` — is a member of a class
that lives in the *client's* package.

The consequence for anyone extending NDIF: **you cannot add a status
server-side.** A new member has to land in nnsight, ship in an nnsight release,
and be installed by the user before the server may emit it. A server that
publishes `{"status": "THROTTLED"}` to a client whose enum lacks that member
fails pydantic validation on the client, which surfaces as a broken remote run
rather than an unknown-status warning.

If you need to convey something new without a client release, put it in
`description` on an existing status, or emit it as a `LOG` — `LOG` is explicitly
"a transient server message, not a lifecycle stage", it is skipped by
`_advance_status` (`src/ndif/common/schema/request.py:99`) so it does not disturb
phase timing, and clients already render it as output.

Everything else on the response is equally shared: `id`, `status`,
`description`, and `data` are `ResponseModel`'s fields. Adding a field
server-side means adding it to nnsight.

## Source, not bytecode

nnsight serializes the traced block by **source text** plus the filtered set of
globals and locals it references, and the server recompiles it under the original
filename and line offsets. Standard cloudpickle would ship *bytecode*, which is
tied to an exact CPython version.

What this buys and what it doesn't:

| Drift | Effect |
|---|---|
| Client Python 3.11, server 3.12 | fine — the source reparses. This is the explicit design goal. |
| Client uses syntax the server's Python can't parse | `SyntaxError` at deserialize, surfaced as an `ERROR` response |
| Client and server nnsight differ | **unsafe** — see below |
| A referenced object from a third-party package whose pickle format changed | unpickling error at deserialize |
| A locally-defined helper the server has never heard of | `ModuleNotFoundError`, unless registered |

The block's *source* is version-independent. The objects it closes over are not:
tensors, tokenizers, config objects, and every nnsight internal in the payload
cross as ordinary (cloud)pickles.

## When client and server nnsight disagree

The failure modes, roughly in order of how commonly they bite:

- **A changed nnsight class layout.** The tracer itself is pickled. If the
  client's `Tracer`/`Interleaver`/`Envoy` gained or lost attributes relative to
  the server's, the payload unpickles into an object the server's code then
  misuses — often an `AttributeError` from deep inside nnsight, reported to the
  user as their own job failing.
- **A changed persistent-id scheme.** The model is *not* shipped; it is
  referenced by persistent id (`"Interleaver"`, `"Module:<path>"`, and
  model-specific ones like `"Tokenizer"`), and the server resolves those ids from
  a map the loaded model supplies. A client that tags ids the server's map does
  not contain raises `UnknownPersistentIdError` at deserialize.
- **Different module paths for the same checkpoint.** The block names modules by
  path (`model.transformer.h[0]`). If the client's transformers version builds a
  different module tree than the server's, the path either doesn't exist or
  points somewhere else. Nothing validates this ahead of time — the block simply
  parks on a location the forward pass never reaches, and the request ends in an
  `OutOfOrderError` or a dangling-worker warning.
- **Semantic drift with no error at all.** A default that changed between
  transformers releases produces different numbers, not an exception.

nnsight ships a first-class tool for this. `nnsight.compare()` fetches the
server's `GET /env` (python version plus installed packages), diffs it against
the local environment, and highlights the packages whose drift breaks
interventions subtly — nnsight, transformers, and torch:

```python
import nnsight
print(nnsight.compare())            # local vs remote table
nnsight.compare().critical_mismatches
```

Server-side, `/env` is a TTL'd, coalesced cache of the controller's environment
(`NDIF_ENV_TTL_S`, default 300; `src/ndif/common/redis/env.py`), so it is cheap
to call and can lag a redeploy by up to five minutes.

## Client-side packages the user needs anyway

The server does the computing, but the client still constructs a model wrapper
locally (on the meta device) to resolve module paths and mint the model key. That
means the client needs enough of the ecosystem to build the *same architecture*
the server loaded.

**PEFT.** Adapters are per-request, not per-deployment: the client instantiates
`TransformersModel(repo, peft="<adapter repo id>")`, nnsight puts `{"peft": ...}`
in the request's `env`, and the actor applies it before every run via
`_remoteable_set_env` (`.../modeling/base.py:294`). Both sides need `peft`
installed, for different reasons:

- **Server** — `peft` is a hard dependency of the `ray` extra
  (`pyproject.toml`), so the model container has it. The adapter is fetched from
  the Hub *by id*; a local adapter directory on the user's machine is invisible
  to the server.
- **Client** — without `peft` the user's meta model has no adapter modules, so
  the paths they write (`...base_model.model.transformer.h[0]...`) don't match
  what the server exposes. A bad adapter id comes back as a normal user-facing
  `ERROR` with the real message.

The same logic applies to any optional package that changes a module tree.
Whatever wraps the module structure server-side must wrap it client-side too.

**Local helper modules.** Code in the block that references a locally-defined
function or class would hit `ModuleNotFoundError` on the server. nnsight's remote
backend calls `pull_env()` before its first request, which auto-registers every
module it finds importable from the working tree but not pip-installed, shipping
their source by value; `nnsight.register(module)` does it explicitly. Installed
packages are *not* registered — the server is expected to have them.

**`.save()` on non-tensors needs a compiled extension — on the server.** The
user's block executes server-side, so `some_list.save()` runs in the model actor
or the runner process, and that form depends on nnsight's optional
`nnsight._c.py_mount` C extension being built at install time. The image installs
`gcc` and `libc6-dev` for exactly this reason (`docker/Dockerfile`); on an image
without a compiler, `x.save()` on a non-tensor raises `AttributeError` remotely
while working fine locally. `nnsight.save(x)` never depends on the mount and is
the portable form.

## Bumping either side

There is no CI and no compatibility matrix in this repo (v0.0.1) — the only
suite is the live-server one under `tests/`, which skips unless the stack is up.
The practical checklist when you bump the server's nnsight lives in
[nnsight integration](../developing/nnsight-integration.md#bumping-the-client);
the short version:

1. Rebuild and bring the stack up (`just ta`), then run `pytest tests/`.
2. Exercise **both** execution paths — a bare `just up` has no Postgres, so every
   request is trusted and the sandbox never runs.
3. Have a client on the *old* nnsight submit a request. If it fails, that is your
   new `NDIF_MIN_NNSIGHT_VERSION`.
4. Set `NDIF_MIN_NNSIGHT_VERSION` to the oldest client you actually intend to
   support, and restart the API.

## Related

- [nnsight integration](../developing/nnsight-integration.md) — the full
  client/server contract: what is on the wire, which nnsight internals the server
  depends on, and the extension points nnsight provides.
- [Auth and limits](../concepts/auth-and-limits.md) — the gate in context with
  API keys and the trusted flag.
- [Status and results](../concepts/status-and-results.md) — the lifecycle those
  shared `Status` values describe.
- [Client-side failures](../errors/client-side-failures.md) — mapping what the
  user sees in nnsight back to a server-side cause.
- [Models and deployment](../operating/models-and-deployment.md) — PEFT adapters
  and model keys from the operator's side.
