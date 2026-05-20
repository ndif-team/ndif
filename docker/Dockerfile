FROM astral/uv:python3.12-trixie-slim

# Sometimes apt-get tries to open a dialog box, this stops it
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
         build-essential git cron curl ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# NAME selects the build flavor:
#   api / ray / dashboard  → per-service image (used by docker-compose)
#   all                    → standalone all-in-one (published to Docker Hub)
ARG NAME
ENV NDIF_SERVICE=${NAME}
WORKDIR /ndif

# Layer 1: torch + torchvision. Largest install, rarely changes — kept
# in its own layer so source edits don't re-download torch.
RUN uv pip install --system torch torchvision

# Layer 2: service requirements. Copy all four .in files so the layer
# key reflects every flavor's deps; install conditionally.
#   NAME=all → api + ray + dashboard requirements (full standalone)
#   else     → just that service's requirements
# Trade-off: changing one service's .in invalidates this layer for the
# other flavors too. Acceptable since `make build` builds all three
# together and shares the cache anyway.
COPY src/ndif/services/api/requirements.in       /ndif/src/ndif/services/api/requirements.in
COPY src/ndif/services/ray/requirements.in       /ndif/src/ndif/services/ray/requirements.in
COPY src/ndif/services/dashboard/requirements.in /ndif/src/ndif/services/dashboard/requirements.in
RUN if [ "$NAME" = "all" ]; then \
        uv pip install --system \
            -r src/ndif/services/api/requirements.in \
            -r src/ndif/services/ray/requirements.in \
            -r src/ndif/services/dashboard/requirements.in; \
    else \
        uv pip install --system -r src/ndif/services/${NAME}/requirements.in; \
    fi

# Layer 3: full source + editable install. Invalidated on any source
# edit, but the install itself is cheap (--no-deps). Editable so
# cli/config.py's load_dotenv finds /ndif/.env.example and the
# dashboard's source-tree frontend_dist default resolves.
COPY . /ndif
RUN uv pip install --system -e . --no-deps \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /root/.cache

# For the all-in-one image, pre-bootstrap redis + minio via the CLI's own
# micromamba path. Skips the slow first-run download; runtime `ndif start`
# finds the binaries already in the ndif-deps env.
RUN if [ "$NAME" = "all" ]; then \
        python -c "from ndif.cli.lib.deps import ensure_redis, ensure_object_store; \
                   assert ensure_redis() and ensure_object_store()"; \
    fi

SHELL ["/bin/bash", "-c"]
CMD if [[ -n "${RAY_ADDRESS}" ]]; then \
      ndif start "${NDIF_SERVICE}" --verbose --worker --ray-address "${RAY_ADDRESS}"; \
    else \
      ndif start "${NDIF_SERVICE}" --verbose; \
    fi
