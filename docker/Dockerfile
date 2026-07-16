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

RUN uv pip install --system torch==2.10.0 torchvision==0.25.0

# mamba-ssm + causal-conv1d as prebuilt CUDA-extension wheels matched to the
# torch install above (torch 2.10 / cu12 / cp312 / cxx11 ABI=TRUE). Pinned by URL
# so the image needs no CUDA toolchain to compile them. Needed for the Nemotron-H
# custom (trust_remote_code=True) triton mamba path; torch 2.10 also gives native
# F.grouped_mm on sm_80 (A100) so the native MoE path has no fp32/bf16 fallback bug.
#
# ABI is load-bearing: PyTorch's Linux pip wheels are new C++11 ABI
# (_GLIBCXX_USE_CXX11_ABI=1) from torch 2.7 on, so these kernels MUST be the
# cxx11abiTRUE variants and the torchX.Y tag MUST match the torch version above.
# A mismatch yields:
#   ImportError: causal_conv1d_cuda...so: undefined symbol: _ZN3c106detail14torchCheckFail...
# If the torch line changes version/cuda/python, swap these for the corresponding
# release wheels (https://github.com/Dao-AILab/causal-conv1d/releases and
# https://github.com/state-spaces/mamba/releases).
RUN uv pip install --system --no-deps \
        https://github.com/Dao-AILab/causal-conv1d/releases/download/v1.6.2.post1/causal_conv1d-1.6.2.post1%2Bcu12torch2.10cxx11abiTRUE-cp312-cp312-linux_x86_64.whl \
        https://github.com/state-spaces/mamba/releases/download/v2.3.2.post1/mamba_ssm-2.3.2.post1%2Bcu12torch2.10cxx11abiTRUE-cp312-cp312-linux_x86_64.whl


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
