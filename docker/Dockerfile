# Single, configurable NDIF service image.
#
# One image runs any NDIF service; pick it at runtime with NDIF_SERVICE
# (api | ray | redis | all, or a space/comma list — resolved by the
# `ndif start --foreground` entrypoint below).
# Build context is the repo root (see docker-compose.yml), so the paths below
# are relative to it.
#
# requirements.txt pins the heavy deps (torch cu124, ray, transformers, ...);
# it's installed first, from its own layer, so ndif/nnsight code changes reuse
# the cached multi-GB install instead of redownloading it. GPU services (ray)
# should be run with `--gpus all`.
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
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends cron gcc libc6-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

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

# `ndif start --foreground` runs the service(s) named by NDIF_SERVICE attached
# to PID 1 (a single service is exec'd directly; several are supervised).
ENTRYPOINT ["ndif", "start", "--foreground"]
