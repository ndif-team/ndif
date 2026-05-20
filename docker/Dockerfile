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

# Layer 1: torch + torchvision. Largest install, rarely changes — kept
# in its own layer so source edits don't re-download torch.
RUN uv pip install --system torch torchvision \
        --extra-index-url https://download.pytorch.org/whl/cu128

# Layer 2: per-service requirements. Copy ONLY the .in files (not the
# rest of the source) so this layer is invalidated only when a
# requirements file changes — not on every source edit.
COPY src/ndif/services/api/requirements.in       /ndif/src/ndif/services/api/requirements.in
COPY src/ndif/services/ray/requirements.in       /ndif/src/ndif/services/ray/requirements.in
COPY src/ndif/services/dashboard/requirements.in /ndif/src/ndif/services/dashboard/requirements.in
RUN uv pip install --system \
        -r src/ndif/services/api/requirements.in \
        -r src/ndif/services/ray/requirements.in \
        -r src/ndif/services/dashboard/requirements.in

# Layer 3: full source + editable install. Invalidated on every source
# edit but the install itself is cheap (--no-deps). Editable so
# cli/config.py's load_dotenv resolves /ndif/.env.example and the
# dashboard's source-tree frontend_dist default works.
COPY . /ndif
RUN uv pip install --system -e . --no-deps \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /root/.cache

# Pre-bootstrap redis + minio via the CLI's own micromamba path. Used by
# the all-in-one (NDIF_SERVICE=all) entrypoint; harmless overhead in
# compose-mode containers (they never use the local binaries).
RUN python -c "from ndif.cli.lib.deps import ensure_redis, ensure_object_store; \
               assert ensure_redis() and ensure_object_store()"

# NDIF_SERVICE chooses what the entrypoint starts at runtime.
#   all (default)         → broker + object-store + ray + api in one container
#                           (the standalone Docker Hub experience)
#   api / ray / dashboard → just that service (used by compose / ECS)
#
# `--build-arg NAME=...` is also accepted and bakes the value in as the
# default — kept so existing CI pipelines that pass NAME=api / NAME=ray
# at build time still produce images that auto-start the right service
# without needing a runtime env override.
ARG NAME=all
ENV NDIF_SERVICE=${NAME}

SHELL ["/bin/bash", "-c"]
CMD if [[ -n "${RAY_ADDRESS}" ]]; then \
      ndif start "${NDIF_SERVICE}" --verbose --worker --ray-address "${RAY_ADDRESS}"; \
    else \
      ndif start "${NDIF_SERVICE}" --verbose; \
    fi
