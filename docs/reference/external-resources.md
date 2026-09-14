---
title: External Resources
one_liner: Where to go when the answer isn't in this repo — nnsight's docs and source, the NDIF site and Discord, the paper, the specific Ray topics NDIF leans on, and the in-repo documents that are authoritative.
tags: [reference]
related: [docs/developing/nnsight-integration.md, docs/gotchas/client-server-versions.md, docs/developing/sandbox-internals.md, docs/developing/ray-service.md, docs/reference/ports.md, docs/developing/controller-internals.md]
sources: [README.md, src/ndif/services/ray/sandbox/ARCHITECTURE.md, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/cluster/deployment.py]
---

# External Resources

## What this covers

The short list of things outside this repo that are worth reading, and the one
sentence that tells you when. Everything here is a pointer — nothing on this page
is a substitute for the code, and where an external doc disagrees with this
repo's code, the code wins.

## NDIF and nnsight

| Resource | Where | Go there when |
|---|---|---|
| NDIF website | [ndif.us](https://ndif.us) | You want the hosted service — model list, API keys, status — rather than a server you run yourself. |
| nnsight documentation | [nnsight.net](https://nnsight.net) | You need the *client* semantics: tracing, `.save()`, `.source`, sessions, `remote=True`. Everything a user writes inside a block is documented there, not here. |
| nnsight source | [github.com/ndif-team/nnsight](https://github.com/ndif-team/nnsight) | You are changing NDIF's serialization, the sandbox's interleaver split, or bumping the client and need to read `intervention/serialization.py`, `intervention/interleaver.py`, `schema/request.py` or `schema/response.py` for real. |
| nnsight agent docs | the `docs/` tree inside that repo | You are an agent debugging a user's `remote=True` failure. Its `docs/gotchas/remote.md` and `docs/gotchas/save.md` cover the client-side half of most "my values came back empty" reports; `docs/remote/` covers env comparison and registering local modules. |
| NDIF Discord | [discord.gg/6uFJmCSwW7](https://discord.gg/6uFJmCSwW7) | The question is about the hosted deployment's state, a model you'd like deployed, or something no code in either repo answers. |
| The paper | [arXiv:2407.14561](https://arxiv.org/abs/2407.14561) | You want the motivation and the research framing — why remote model internals at all — rather than an implementation detail. It describes the project, not this server's current architecture. |

The paper's citation, as committed in `README.md`:

```bibtex
@article{fiottokaufman2024nnsightndifdemocratizingaccess,
      title={NNsight and NDIF: Democratizing Access to Foundation Model Internals},
      author={Jaden Fiotto-Kaufman and Alexander R Loftus and Eric Todd and Jannik Brinkmann and Caden Juang and Koyena Pal and Can Rager and Aaron Mueller and Samuel Marks and Arnab Sen Sharma and Francesca Lucchetti and Michael Ripa and Adam Belfki and Nikhil Prakash and Sumeet Multani and Carla Brodley and Arjun Guha and Jonathan Bell and Byron Wallace and David Bau},
      year={2024},
      eprint={2407.14561},
      archivePrefix={arXiv},
      primaryClass={cs.LG},
      url={https://arxiv.org/abs/2407.14561},
}
```

## Ray

NDIF uses a narrow slice of Ray, and reading around it wastes time. **NDIF does
not use Ray Serve** — model deployments are plain detached actors — so ignore
every Serve page you find.

| Ray topic | Go there when |
|---|---|
| Ray Core: actors, `lifetime="detached"`, namespaces, `ray.get_actor` | You are reading the controller. Deployments are `actor_class.options(name=..., namespace="NDIF", lifetime="detached")` (`.../cluster/deployment.py:192-197`); every deploy/evict is an actor create or `ray.kill`. |
| Ray Client (`ray://`) | You are debugging why the API can't reach the cluster. The dispatcher holds the one client connection; port 10001 is Ray's own default and NDIF never overrides it. |
| `ray start` flags and the cluster port map | You are adding a node. `services/ray/start.sh` passes five port flags and lets Ray default the rest — the raylet, node-manager, and worker port range are Ray's business, and Ray's port documentation is the only place they're enumerated. |
| Custom resources (`--resources`) | You are reading `resources.py`. NDIF advertises `cuda_memory_bytes`, `cpu_memory_bytes`, `head=10` and an optional node label, and the controller reads them back off the node list. |
| `runtime_env` / `env_vars` | You are wondering how a model actor got its Redis address. The controller copies its provider config into each actor's `runtime_env`. |
| The plasma object store and `/dev/shm` | You hit a shared-memory error or unexplained spilling. See [GPU and memory gotchas](../gotchas/gpu-and-memory.md) for the `shm_size` requirement. |
| Ray metrics / Prometheus export | You are wiring monitoring. `--metrics-export-port` is what Prometheus scrapes; the NDIF variable that sets it is `NDIF_RAY_METRICS_PORT` (`start.sh:66`). |

Ray's version is pinned in `requirements.txt`; read the docs for **that** version,
not `latest` — port defaults and actor options have moved between releases.

## Other upstream projects worth reading at the source

- **accelerate** — `device_map="balanced"` and `max_memory` decide where a
  model's layers land. When a deploy fails `verify_device_placement`, accelerate
  chose to offload to CPU rather than error; its dispatch documentation explains
  why.
- **cloudpickle** — `register_pickle_by_value` is what `nnsight.register` wraps,
  and cloudpickle's persistent-id and by-value semantics underpin the whole
  payload format.
- **boto3 / S3 presigned URLs** — a presigned URL is an HMAC over the request
  including the host. That single fact explains
  `NDIF_OBJECT_STORE_PUBLIC_URL`; see
  [Networking and compose gotchas](../gotchas/networking-and-compose.md).
- **PEFT** — adapters are applied per request through nnsight's
  `_remoteable_set_env`, and the client needs the package too.

## In-repo documents that are authoritative

Prefer these over anything reconstructed from memory — they are maintained
alongside the code.

| Path | What it is |
|---|---|
| `src/ndif/services/ray/sandbox/ARCHITECTURE.md` | The design note for the split interleaver and the runner protocol. Current and good; the authority on *why* the sandbox is shaped this way. [Sandbox internals](../developing/sandbox-internals.md) summarizes it and adds request-level context. |
| `README.md` | Quickstart and the `NDIF_*` table. Mostly accurate, with known drift: it describes Ray Serve (NDIF uses detached actors) and calls `NDIF_MODEL_CACHE_PERCENTAGE` a GPU knob (it scales CPU RAM). |
| `docker/docker-compose.yml` | The most honest description of how the services are wired; its comments explain several non-obvious settings. |
| `pyproject.toml` / `requirements.txt` | What is actually installed, and comments explaining why each non-obvious dependency is there (`zstandard`, `peft`). |
| `justfile` | Every supported operational command against the dev stack. |

## Material that looks relevant and isn't

Search results will hand you three kinds of stale answer. Recognizing them saves
an afternoon:

- **Ray Serve tutorials and `serve.deployment` examples.** NDIF's model
  deployments are detached Ray actors created and killed by the controller.
  `ray[serve]` appears only as an install hint in `ndif doctor`. Nothing in this
  repo calls Serve.
- **Writeups of NDIF's previous server.** An older design used Ray Serve
  applications and sandboxed user code *in-process* behind an import/attribute
  whitelist. Both are gone: execution is a detached actor, and isolation is
  process-based — a separate runner process per request, driven over a Unix
  socket. Mine old material for motivation only, and verify every structural
  claim against the code here.
- **Anything describing VM or microVM isolation.** That approach was removed.
  A few stale docstrings under `src/ndif/services/ray/sandbox/` still mention a
  "VM twin"; ignore them and read
  [Sandbox internals](../developing/sandbox-internals.md).

## Related

- [nnsight integration](../developing/nnsight-integration.md) — the client/server
  contract, and the checklist for bumping nnsight.
- [Client and server version coupling](../gotchas/client-server-versions.md) —
  what drift between the two actually breaks.
- [Ray service](../developing/ray-service.md) — `start.sh`, head versus worker,
  and the resources NDIF advertises.
- [Ports](ports.md) — every port, including the Ray ones this page defers to
  Ray's documentation for.
