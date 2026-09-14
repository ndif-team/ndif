# Single, configurable NDIF service image.
#
# One image runs any NDIF service; pick it at runtime with NDIF_SERVICE
# (api | ray | redis | minio | dashboard | all, or a space/comma list). The
# entrypoint is the `ndif` CLI and the default command is `start --foreground`,
# so:
#
#   docker run --gpus all ndif/ndif                 # NDIF_SERVICE=all: the whole stack
#   docker run -e NDIF_SERVICE=api ndif/ndif        # one service (compose does this)
#   docker run ndif/ndif doctor                     # any other CLI command
#   docker run ndif/ndif --version
#
# Build context is the repo root (see docker-compose.yml), so the paths below
# are relative to it.
#
# requirements.txt pins the heavy deps (ray, transformers, ...); it's installed
# from its own layer, so ndif/nnsight code changes reuse the cached multi-GB
# install instead of redownloading it. GPU services (ray) must be run with
# `--gpus all`.

# MinIO no longer publishes standalone binaries (dl.min.io returns 410 and the
# GitHub releases carry no assets), so the server binary is copied out of the
# official image. quay.io, not Docker Hub: the Hub repository's tag list is
# gone ("object not found"), quay.io still serves every release. Pinned to a
# release, not `latest`, so a rebuild is reproducible.
ARG MINIO_IMAGE=quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z
FROM ${MINIO_IMAGE} AS minio

FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NDIF_SERVICE=all

WORKDIR /app

# redis-server + minio: the in-container stack for NDIF_SERVICE=all (the CLI
# spawns both; compose runs them as their own containers instead and never
# starts these). cron: the dashboard service runs the monitor + reconcile jobs
# from cron; its start.sh only wires them up when `cron` is on PATH. gcc +
# libc6-dev: let nnsight's optional `nnsight._c.py_mount` C extension compile
# when nnsight installs from an sdist (it mounts `.save()` on objects; without a
# compiler setuptools skips it and `value.save()` breaks on the server). git:
# lets requirements.txt point at a git ref when a fix hasn't reached PyPI.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends cron gcc libc6-dev git redis-server \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY --from=minio /usr/bin/minio /usr/local/bin/minio

# torch, from a build arg rather than requirements.txt: the wheel you need is a
# property of the *host's* CUDA driver, not of this repo. A CUDA 13 wheel on a
# 12.x driver refuses to initialise ("the NVIDIA driver on your system is too
# old"), and torch.cuda.is_available() silently returns False rather than
# failing the build. Any 12.x wheel runs on any 12.x driver >= 525 (CUDA minor
# version compatibility), so cu126 is the widest safe default.
#
# The default is the latest torch on the cu126 index. Both are build args:
#   docker build --build-arg TORCH_CUDA=cu130 .                 # another CUDA line
#   docker build --build-arg TORCH_SPEC="torch==2.11.0" .       # pin the version
#
# --index-url, not --extra-index-url: this layer installs only torch, and
# pointing pip at the CUDA index alone is what stops it picking PyPI's default
# wheel, which currently means CUDA 13.
#
# This is how the upstream images do it too: pytorch/pytorch publishes one tag
# per (torch, cuda, cudnn) combination from a single Dockerfile, and NGC does the
# same. Published NDIF images follow suit: one tag per CUDA line
# (`<version>-cu126`, `<version>-cu130`), see .github/workflows/publish_docker.yml.
# Not cu128: PyTorch's cu128 index stopped at torch 2.11 while cu126 and cu130
# both carry the current release, and cu130 covers Blackwell.
#
# Its own layer, and first, because it is the largest single install — a
# requirements.txt change shouldn't re-download it.
ARG TORCH_SPEC=torch
ARG TORCH_CUDA=cu126
RUN pip install "${TORCH_SPEC}" \
      --index-url "https://download.pytorch.org/whl/${TORCH_CUDA}"

# Heavy, rarely-changing dependency layer — cached unless requirements.txt
# itself changes.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# The ndif package with the service + telemetry + auth + dashboard extras
# (api + ray + metrics + postgres + dashboard); also installs the `ndif` console
# script. --no-deps because every dependency is already pinned in requirements.txt
# above (extras only select what to declare), so a src change reinstalls only the
# package itself.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-deps ".[api,ray,metrics,postgres,dashboard]"

# The `ext` extra: packages user-submitted traced blocks are allowed to import,
# rather than ones NDIF itself needs. Read out of pyproject.toml and installed as
# a plain requirement list — `pip install ".[ext]"` would be a no-op here, since
# pip sees ndif already installed at this version and never resolves the extra.
# Reading the list keeps pyproject.toml the single source of truth, so a
# non-Docker install (`pip install ndif[ext]`) gets exactly the same surface.
# Own layer: widening it doesn't rebuild the stack above.
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['optional-dependencies']['ext']))" > /tmp/ext.txt \
    && pip install -r /tmp/ext.txt \
    && rm /tmp/ext.txt

# What this image resolved to. The versions that decide the server's behaviour
# (torch + its CUDA line, transformers, nnsight, ray) are only known after the
# installs above, so they are recorded in the image at /etc/ndif/build.json —
# the same data `ndif version` prints at runtime — so a tagged image carries a
# record of what it was built with. The build-time inputs go on OCI labels.
RUN ndif version --write /etc/ndif/build.json

# Build-time inputs. NDIF_VERSION / VCS_REF / BUILD_DATE are passed by the
# publish workflow; a local `docker build` leaves them unset, which is fine.
ARG NDIF_VERSION=
ARG VCS_REF=
ARG BUILD_DATE=
LABEL org.opencontainers.image.title="NDIF" \
      org.opencontainers.image.description="The NDIF server: runs nnsight intervention code against large models on shared GPUs." \
      org.opencontainers.image.url="https://ndif.us" \
      org.opencontainers.image.source="https://github.com/ndif-team/ndif" \
      org.opencontainers.image.documentation="https://github.com/ndif-team/ndif/blob/main/docker/README.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${NDIF_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      us.ndif.torch.cuda="${TORCH_CUDA}" \
      us.ndif.torch.spec="${TORCH_SPEC}"

# Ports the services listen on (documentation only; publish with -p). The Ray
# GCS/object-manager ports and the dashboard agent are internal.
#   8001 API   9000 MinIO S3   9001 MinIO console   8081 admin dashboard
#   8265 Ray dashboard   10001 Ray client   6379 Redis
EXPOSE 8001 9000 9001 8081 8265 10001 6379

# Model weights go here (HF_HOME); mount the host cache so they persist.
VOLUME ["/root/.cache/huggingface"]

# `ndif start --foreground` runs the service(s) named by NDIF_SERVICE attached
# to PID 1 (a single service is exec'd directly; several are supervised).
ENTRYPOINT ["ndif"]
CMD ["start", "--foreground"]
