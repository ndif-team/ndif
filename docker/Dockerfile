# Single, configurable NDIF service image.
#
# One image runs any NDIF service; pick it at runtime with NDIF_SERVICE
# (api | ray | redis | all, or a space/comma list — resolved by the
# `ndif start --foreground` entrypoint below).
# Build context is the repo root (see docker-compose.yml), so the paths below
# are relative to it.
#
# requirements.txt pins the heavy deps (ray, transformers, ...); it's installed
# from its own layer, so ndif/nnsight code changes reuse the cached multi-GB
# install instead of redownloading it. GPU services (ray) should be run with
# `--gpus all`.
FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NDIF_SERVICE=api

WORKDIR /app

# cron: the dashboard service (NDIF_SERVICE=dashboard) runs the monitor +
# reconcile jobs from cron; its start.sh only wires them up when `cron` is on
# PATH. Harmless for the other services. Its own layer so it stays cached.
# gcc + libc6-dev: let nnsight's optional `nnsight._c.py_mount` C extension
# compile when nnsight installs from an sdist (it mounts `.save()` on objects;
# without a compiler setuptools skips it and `value.save()` breaks on the server).
# git: required to install nnsight from GitHub in requirements.txt.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends cron gcc libc6-dev git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# torch, from a build arg rather than requirements.txt: the wheel you need is a
# property of the *host's* CUDA driver, not of this repo. A CUDA 13 wheel on a
# 12.x driver refuses to initialise ("the NVIDIA driver on your system is too
# old"), and torch.cuda.is_available() silently returns False rather than
# failing the build. Any 12.x wheel runs on any 12.x driver >= 525 (CUDA minor
# version compatibility), so cu126 is the widest safe default.
#
# The default is the latest torch on the cu126 index. Both are build args:
#   docker build --build-arg TORCH_CUDA=cu128 .                 # another CUDA line
#   docker build --build-arg TORCH_SPEC="torch==2.11.0" .       # pin the version
#
# --index-url, not --extra-index-url: this layer installs only torch, and
# pointing pip at the CUDA index alone is what stops it picking PyPI's default
# wheel, which currently means CUDA 13.
#
# This is how the upstream images do it too: pytorch/pytorch publishes one tag
# per (torch, cuda, cudnn) combination from a single Dockerfile, and NGC does the
# same. If you publish NDIF images, build one tag per CUDA line the same way
# rather than trying to make one image cover every driver.
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

# `ndif start --foreground` runs the service(s) named by NDIF_SERVICE attached
# to PID 1 (a single service is exec'd directly; several are supervised).
ENTRYPOINT ["ndif", "start", "--foreground"]
