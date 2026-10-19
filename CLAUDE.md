# NDIF — Agent Guide

NDIF is the server behind [nnsight](https://nnsight.net): it runs user-submitted
intervention code against large models on shared GPUs. This repo is the server;
the client is nnsight, whose docs live in its own repo.

## Rules

- Branch off `dev` and open PRs into `dev`.
- Docs change in the same PR as the code that invalidates them.
- Cite code by path + symbol (`src/ndif/services/api/auth.py` `verify_api_key`),
  never by line number. `pytest tests/test_docs.py` checks paths, symbols and links.
- The rest is in [CONTRIBUTING.md](CONTRIBUTING.md).

## Read this before anything else

Where user code runs depends on one boolean, `request.trusted`, stamped at ingress
from the API key. A trusted block runs **inside the model actor process**, next to
the weights; an untrusted one runs in a separate runner process driven over a Unix
socket. Whether a model runs its own repo code is in its model key
(`trust_remote_code`), not a deploy flag; only a request whose key carries the
`trust_remote_code` or `trusted` tag may deploy such a model. With auth off (no
`NDIF_POSTGRES_URL`) a client-supplied `trusted` is honored and an unspecified one
defaults to trusted — so a bare `just up` is unauthenticated **and** runs user code
in-process. Send `trusted: false` to exercise the runner path without Postgres.
The sandbox is process separation, not a security boundary. Read
[docs/concepts/sandbox-execution.md](docs/concepts/sandbox-execution.md) before
changing or describing it.

## Where to start

[docs/index.md](docs/index.md) has the by-task table and the section map. If you
are new here, read [docs/concepts/request-lifecycle.md](docs/concepts/request-lifecycle.md)
once; every other page zooms into one of its hops.

## Cheat sheet

- **No Ray Serve.** Deployments are detached Ray actors named
  `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace. Ignore Serve docs.
- **An actor's exception arrives as a `RayTaskError`; read `.cause`.** Over Ray
  Client a bare `isinstance` against the original type silently matches nothing.
- **The API can't reach Ray; Redis is the only durable handoff.** Only the
  dispatcher, a child of the gunicorn master, holds a Ray client. A popped request
  lives in its memory, so an API restart drops in-flight work, and
  `ray:connected` has no TTL — health endpoints pass after the dispatcher dies.
- **Source is baked into the image.** Run `just ta` after a code change.
  `just ta <svc>` tears the **whole** stack down and brings back only that service and its dependencies; HOT deployments are lost.
- **The published image runs the core stack in one container.** `NDIF_SERVICE`
  defaults to `all`: redis, rustfs, ray and the API. The dashboard is optional and
  is not part of `all`.
- **Provider defaults are `localhost`.** Inside the `ray` container nothing listens
  on `localhost:6379`, so compose sets `NDIF_REDIS_URL` explicitly. Ray's head port
  is 6385 from both the CLI and `start.sh`.

## Commands

```bash
just up                    # build + start the compose stack, detached
just ta                    # down -> rebuild -> up, after a code change
just logs api              # follow one service
pytest tests/              # everything; live-server tests skip unless localhost:8001 is up
pytest tests/test_docs.py  # the docs gate; no server needed
```
