---
title: Release audit, September 2026 (ndif 0.1.0 / nnsight 0.8)
one_liner: What was exercised, what broke, what was fixed and what is still open, from a full pass over the three self-host routes, the docs, the Docker image and the skills plugin before the 0.1.0 release.
tags: [developing, release, docker, cli, docs]
related: [docs/operating/quickstart.md, docs/operating/troubleshooting.md, docker/README.md, docs/developing/contributing.md, docs/developing/testing.md]
sources: [docker/Dockerfile, docker/docker-compose.yml, justfile, requirements.txt, src/ndif/services/ray/start.sh, src/ndif/cli/service.py, src/ndif/cli/commands/version.py, src/ndif/cli/commands/doctor.py, .github/workflows/publish_docker.yml, .github/workflows/publish.yml]
---

# Release audit, September 2026

## What this covers

A single pass, on 2026-09-14, over everything a person or an agent touches when
they run NDIF for themselves, done against the `dev` branch as it stood after
the 0.8 rewrite (`4d4045a`) and shipped as `release/0.1.0`. Three routes were
built and run end to end, every documentation page was checked against the
source, a new Docker image was built for two CUDA lines, and an `ndif` skills
plugin was written. This page is the list of what was found. Items are numbered
so they can be referred to; **fixed** means the fix is on the release branch,
**open** means it is not.

The routes, and how each was verified:

| Route | Where | Result |
|---|---|---|
| `docker run --gpus all ndif/ndif` (whole stack, one container) | this machine, RTX A6000 | `/connected` in ~30 s; gpt2 trace completes in 8–9 s including on-demand deploy; results over 4 MiB come back through MinIO on port 9000; `NDIF_SERVICE="all dashboard"` serves the admin UI; `docker exec … ndif status/queue/doctor/version` work |
| `just up` / `just ta` compose stack from the checkout | this machine | same trace, with both a client on the pinned nnsight (the image itself) and a client on a newer 0.8 dev checkout |
| From source, no Docker (`ndif start`) | hakone, fresh conda env, python 3.12, 2 of 8 A100s | `conda install -c conda-forge redis-server minio-server`, torch from the cu126 index, `pip install -e ".[api,ray,metrics,postgres,dashboard,ext]"`, `ndif doctor` all green, `ndif start` with `NDIF_HOME`/`NDIF_RAY_TEMP_DIR`/`NDIF_OBJECT_STORE_URL`/`CUDA_VISIBLE_DEVICES` overrides, trace completes |

## Bugs found and fixed

### The published image and the Dockerfile

1. **The image could not run the stack it advertised.** `docker/Dockerfile`
   installed neither `redis-server` nor `minio`, so `NDIF_SERVICE=all` — the
   one-container mode the Docker Hub README led with — died at once with
   `redis: cannot run 'redis-server'`. The image now installs `redis-server`
   from apt and copies `minio` out of the pinned MinIO image.
2. **The Docker Hub README described the pre-0.8 server**: ports 5001/27018,
   `NDIF_DEV_MODE`, `nnsight.LanguageModel`, "any key works". Replaced by
   `docker/README.md`, which the publish workflow pushes to Docker Hub.
3. **`NDIF_SERVICE` defaulted to `api`**, so a bare `docker run` started only the
   API, which answered 503 forever. Default is `all`.
4. **`docker run ndif/ndif doctor` was impossible**: the entrypoint was the whole
   `ndif start --foreground`, so any argument was appended to `start`. The
   entrypoint is `ndif`, the default command `start --foreground`.
5. **`NDIF_SERVICE="all dashboard"` failed** with `unknown service(s): all`.
   `resolve_targets` (`src/ndif/cli/service.py`) expands `all` inside a list.
6. **No way to ask an image what it carries** short of `pip freeze`. Added
   `ndif version` (ndif, nnsight, torch and its CUDA line, transformers, ray,
   peft, accelerate, python; `--json-output`; `--write`), recorded at build
   time in `/etc/ndif/build.json`, plus OCI labels, `EXPOSE`, and a `VOLUME`
   for the Hugging Face cache. `ndif doctor` reports the same versions.
7. **No publish path to Docker Hub** — CI only pushed to ECR. Added
   `.github/workflows/publish_docker.yml` (on a `v*` tag: `<ver>-cu126`,
   `<ver>-cu130`, `<ver>`, `latest`, and the README). The PyPI workflow
   `publish.yml` existed on `main` but was dropped by the rewrite; restored.
8. **The cu128 torch index is stale.** It stops at torch 2.11 while cu126 and
   cu130 both carry 2.14, so a cu128 build silently paired an old torch with
   transformers 5.17. The publish matrix is cu126 + cu130.
9. `.dockerignore` shipped `docs/`, `tests/` and the Grafana dashboards into
   the build context. Excluded.
10. **The dashboard's cron jobs assumed compose hostnames.** `dashboard/start.sh`
    defaulted the crons' `NDIF_API_URL` to `http://api:8001` while its own
    uvicorn defaulted to `localhost:8001`; in one container the monitor and
    reconcile crons pointed at a host that does not exist. Both default to
    `localhost`.

11. **`ndif doctor` passed on an image whose torch could not use the GPU.** It
    only asked `nvidia-smi`; a `-cu130` image on a 12.5 driver listed the card
    and reported "All checks passed" while `torch.cuda.is_available()` was
    False (torch: "The NVIDIA driver on your system is too old"). Doctor now
    also checks torch and names both CUDA versions with the fix.

### Upstream changes that broke a fresh checkout

12. **`minio/minio` on Docker Hub is no longer pullable at all**
    (`docker manifest inspect minio/minio:latest` fails, the tag list returns
    "object not found"). A fresh `just up` on the unmodified repo failed on the
    `minio` service. Compose and the Dockerfile now use
    `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z`.
13. **MinIO no longer publishes standalone binaries** (`dl.min.io` returns 410;
    the GitHub releases carry no assets), which made `ndif doctor`'s hint
    "install the MinIO server binary" impossible to follow on the from-source
    route. `conda install -c conda-forge minio-server` works and is what the
    hint and the docs now say; copying the binary out of the quay image is the
    fallback.
14. **nnsight was installed from the tip of a git branch**, so two images built
    from the same ndif commit could carry different nnsight code, and the image
    reported `0.7.1.dev243`. `requirements.txt` pins `nnsight>=0.8.0rc1,<0.9`
    from PyPI (the 0.8 branch tip is contained in the rc1 tag). ndif's own
    version was a hardcoded `0.0.1` in `pyproject.toml`; it is now derived from
    the git tag by setuptools-scm, as nnsight's is, and passed into the image
    build (`NDIF_VERSION`) since the build context has no `.git`.

### The compose route

15. **`just up` bind-mounted whatever nnsight the shell's python could import**,
    editable or not, any Python version. With a base conda python 3.13 holding
    nnsight 0.7.0, both `api` and `ray` died on
    `ModuleNotFoundError: No module named 'engineio'` — an error that never
    mentions nnsight. The justfile now mounts only an editable checkout (a path
    outside `site-packages`) or an explicit `NNSIGHT_PATH`, and `just nnsight`
    prints the decision.
16. **A stale image looks healthy until the client is newer than it.** Compose
    only builds when an image is missing, so `just up` after pulling new code
    ran three-week-old images; the symptom was a version-skew error
    (`Can't get attribute 'GeneratorEnvoy'`) on the sandbox path only. Already
    documented as "use `just ta`"; the troubleshooting page now names the
    symptom.

### Every route

17. **Half of all fresh Ray client connections failed.** `ndif status`, `ndif
    deploy`, and every dashboard page waited 40 s and then raised `Starting
    Ray client server failed`, with the `.err` file it named empty. Measured 9
    failures in 17 connections in a container and 1 in 6 on bare metal. The Ray
    client proxier forks a per-client server; grpc logged `Other threads are
    currently calling into gRPC, skipping fork() handlers` on every spawn and
    the child died within 100 ms. The same command run by hand never failed.
    `ray/start.sh` now exports `GRPC_ENABLE_FORK_SUPPORT=0` before `ray start`:
    0 failures in 16 connections afterwards, across docker and bare metal
    (Ray 2.55.1, grpcio 1.84.0). The API's dispatcher was already retrying in a
    loop, which is why the API itself only booted slowly.
18. **The API logs a full traceback once a second while Ray boots** (`Error
    connecting to Ray`, `queue/dispatcher.py`). Expected for the first 30–60 s
    and now documented as such, but it reads as a crash. **Open**: one warning
    with backoff would be better.

### Source comments the docs audit caught

18. `api/auth.py`: `PRIORITY_TAG` said priority requests are prepended to the
    queue; they form a rank group and stay FIFO among themselves
    (`RequestQueue.rank`). `queue/config.py`: change-log prose in a docstring.
    `dashboard/jobs/util.py`: referred to a `services/monitor` that does not
    exist. `sandbox/model.py`: an orphaned half-sentence, and a runner-memory
    figure that disagreed with two docs. `logging_setup.py`: the example log
    line cited a line that logs an eviction. All corrected; the runner figure
    was re-measured (7 runners × ~483 MB PSS ≈ 3.4 GB for gpt2) and the three
    places now agree.

## The documentation pass

Three audits ran in parallel, one per audience — operators (`README.md`,
`docs/operating/`, `docs/runbooks/`, `docs/gotchas/`), developers
(`docs/developing/`, `docs/concepts/`) and reference (`CLAUDE.md`,
`docs/reference/`, `docs/errors/`) — each verifying every claim and every
`file:line` citation against `src/`. Roughly 1,000 citations were checked and
several hundred repointed; the 0.8 rewrite had moved most files (`controller.py`
by ~190 lines, the sandbox by a whole module split). The claims that were
actually wrong, rather than merely drifted:

- Torch was said to come from `requirements.txt` as cu124; it comes from a
  build arg, default cu126. "There is no CI" in five places; three workflows
  exist, none runs tests. Version 0.0.1 in three places.
- `NDIF_SERVICE` default `api`; `NDIF_MODEL_CACHE_PERCENTAGE` called a GPU
  knob (host RAM); `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` given a default of
  3600 (it is unset, meaning no cap) in four pages; `NDIF_RAY_SERVE_PORT` (the
  variable is `NDIF_RAY_METRICS_PORT`); `NDIF_API_KEY` described as a CLI key
  (only the monitor cron reads it).
- "Auth off means every request is trusted" — an explicit `trusted: false` is
  honoured; only an unspecified one defaults to trusted.
- The runner "inherits a copy of the actor's environment" — it starts from the
  `RUNNER_ENV` allowlist. `spawn` "sends stderr to DEVNULL" — it never does.
  `RayTaskError` "still satisfies isinstance" — the opposite, which is why
  `replica.py` reads `.cause`.
- Results "are always referenced by presigned URL, never embedded" — under
  `NDIF_MAX_SOCKET_RESULT_BYTES` (4 MiB) they ride on the response.
- The Ray head port fallback was said to be 6379 colliding with Redis; both the
  CLI and `start.sh` default to 6385.
- The dashboard UI "must be built on the host" — `frontend/dist/` is committed
  and packaged. `Replica.provision` "calls `controller.deploy`" — it calls
  `scale`. `DeploymentConfig` was missing four fields in the schema reference.
  `checkpoint-description-proposal.md` was filed as unimplemented; it shipped.
- `docs/gotchas/` was not linked from `CLAUDE.md` at all; `docs/errors/` had no
  entry for the two most-pasted messages (`Your request payload could not be
  read`, `The model architecture on this server doesn't match`).

`README.md` and `docs/operating/quickstart.md` now lead with the three routes
in order; `CLAUDE.md` routes "run the published image" to `docker/README.md`.

## Open items

- **nnsight (client side).** Inside a trace block, a list built by a
  comprehension or by `.append()` is not bound in the caller's frame after the
  block, locally or remotely — only plain `name = value.save()` assignments
  come back. Reproduced on 0.8.0 and 0.8.0rc1. Belongs in the nnsight
  `debugging` skill's "empty saved lists" entry if it is not already there.
- **The API's Ray-connect log spam** (item 18).
- **Non-root container.** The image runs as root and bind-mounts the host's
  Hugging Face cache into `/root/.cache/huggingface`; a rootless image would
  need a UID convention for that mount and for `NDIF_HOME`. Deferred, since it
  changes every route's volume instructions.
- **No `HEALTHCHECK` in the Dockerfile.** One image serves five services with
  different readiness signals; the compose file carries a `/ping` check for
  `api` instead. A per-service `ndif healthz` would let the image own it.
- **CI runs no tests.** The suite needs a running stack; a GPU runner or a
  CPU-only subset (`tests/test_placement.py`, `test_node_registry.py`,
  `test_replica_wait.py`, `test_fanout.py` run without a server) could gate
  `main`.
- **Ray refuses to schedule on a >95 %-full filesystem** (raylet
  `file_system_monitor`). Hit on hakone; the from-source docs now say to point
  `NDIF_RAY_TEMP_DIR` elsewhere. There is no NDIF-side check.
- **`ndif status` echoes raylet warnings** into its own output when the Ray
  session logs something (the disk warning above). Cosmetic.
- **Two `*-proposal.md` pages under `docs/developing/`** describe shipped
  designs; they are labelled as design records now but keep their names so
  links do not break.

## Releasing 0.1.0

1. Merge `release/0.1.0` into `dev`, then `dev` into `main`.
2. Add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token
   with read/write on the `ndif` organisation) as repository secrets.
   `PYPI_API_TOKEN` and `AWS_IAM_ROLE` already exist.
3. Publish a GitHub release with a new tag `v0.1.0`. The tag is the version:
   there is nothing to bump in `pyproject.toml`. Creating the release pushes
   the tag, which runs `publish_docker.yml` (cu126 and cu130 on GitHub's
   runners; pushes `ndif/ndif:0.1.0-cu126`, `0.1.0-cu130`, `0.1.0`, `latest`
   and the README; ~25 minutes per line the first time), and the release event
   runs `publish.yml` (sdist + wheel, `ndif==0.1.0` to PyPI). A pre-release
   tag such as `v0.2.0rc1` publishes only its own image tags and leaves
   `latest` alone.
5. Images built here are equivalent to what the workflow produces
   (`ndif/ndif:0.1.0-cu126`, `0.1.0-cu130`, `0.1.0`, `latest` are tagged
   locally) and can be pushed by hand with `docker push` after `docker login`
   if the workflow is not ready.

## Fresh-user verification of the published artifacts

After the release, two agents each ran the same scenario on a single-GPU
machine with only the public artifacts, the `ndif` skills plugin and the
internet — no access to a checkout: bring up a server, trace gpt2 remotely,
bring back more than 5 MB, pre-deploy SmolLM2-135M, inspect queue and
deployments, shut down cleanly.

| Route | Steps | Time | What broke or misled |
|---|---|---|---|
| `docker run ndif/ndif` | 6/6 | ~8 min | Nothing blocked. Never opened the docs or the Hub page; the two skills carried it. The metrics provider streamed a connection-refused traceback into the client's first trace (fixed: #287). The `jq` model-key recipe did not run in the image. Hundreds of COLD models from the mounted HF cache, unexplained. No shutdown guidance. |
| `pip install ndif`, no Docker | 5/6 + 1 partial | ~14 min | `pip install ndif` alone installed nnsight 0.7.0 (fixed: #288). Every route-3 command assumed a checkout. `NDIF_RAY_TEMP_DIR` under a long path killed Ray at start on the 107-byte socket limit while `ndif start` said ✓ and the skill said to wait 60–90 s (fixed: #288). `ndif stop` left Ray's daemons running (fixed: #288). conda-forge line stopped on the anaconda ToS prompt. |

Everything the skills warned about that mattered — publish port 9000, plain
assignments only inside a trace block, `/connected` as the readiness signal,
`NDIF_DEPLOYMENTS` takes model keys — paid off in both runs. The skill
corrections are in ndif-team/skills#11.

## The skills plugin

See `plugins/ndif/` in the skills repo — four skills (`ndif-selfhost`,
`ndif-operate`, `ndif-troubleshoot`, `ndif-develop`), written from the pages
above after they were corrected, registered next to the `nnsight` plugin.
