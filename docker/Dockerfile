FROM astral/uv:python3.12-trixie-slim

# Sometimes apt-get tries to open a dialog box, this stops it
ENV DEBIAN_FRONTEND=noninteractive

# build-essential + git: for any wheels that need to compile.
# cron: the dashboard service runs monitor + reconcile crons.
# curl + ca-certificates: micromamba's installer + redis/minio bootstrap.
RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
         build-essential git cron curl ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ndif

RUN uv pip install --system torch torchvision --index-url https://download.pytorch.org/whl/cu124

# mamba-ssm + causal-conv1d as prebuilt CUDA-extension wheels matched to the
# torch install above (torch 2.6 / cu12 / cp312 / pre-cxx11 ABI). Pinned by URL
# so the image needs no CUDA toolchain to compile them. If the torch line above
# changes version/cuda/python, swap these for the corresponding release wheels
# (https://github.com/Dao-AILab/causal-conv1d/releases and
# https://github.com/state-spaces/mamba/releases).
RUN uv pip install --system --no-deps \
        https://github.com/Dao-AILab/causal-conv1d/releases/download/v1.6.2.post1/causal_conv1d-1.6.2.post1%2Bcu12torch2.6cxx11abiFALSE-cp312-cp312-linux_x86_64.whl \
        https://github.com/state-spaces/mamba/releases/download/v2.3.2.post1/mamba_ssm-2.3.2.post1%2Bcu12torch2.6cxx11abiFALSE-cp312-cp312-linux_x86_64.whl


COPY src/ndif/services/api/requirements.in       /ndif/src/ndif/services/api/requirements.in
COPY src/ndif/services/ray/requirements.in       /ndif/src/ndif/services/ray/requirements.in
COPY src/ndif/services/dashboard/requirements.in /ndif/src/ndif/services/dashboard/requirements.in
RUN uv pip install --system \
        -r src/ndif/services/api/requirements.in \
        -r src/ndif/services/ray/requirements.in \
        -r src/ndif/services/dashboard/requirements.in


COPY . /ndif
RUN uv pip install --system -e . --no-deps \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /root/.cache

RUN python -c "from ndif.cli.lib.deps import ensure_redis, ensure_object_store; \
               assert ensure_redis() and ensure_object_store()"


ARG NAME=all
ENV NDIF_SERVICE=${NAME}

SHELL ["/bin/bash", "-c"]
CMD if [[ -n "${RAY_ADDRESS}" ]]; then \
      ndif start "${NDIF_SERVICE}" --verbose --worker --ray-address "${RAY_ADDRESS}"; \
    else \
      ndif start "${NDIF_SERVICE}" --verbose; \
    fi
