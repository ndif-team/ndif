FROM astral/uv:python3.12-trixie-slim AS ndif_base

# Sometimes apt-get tries to open a dialog box, this stops it
ENV DEBIAN_FRONTEND=noninteractive

# Install base utilities in single layer with cleanup
RUN apt-get update -y \
    && apt-get install -y build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy and install base requirements
COPY src/services/base/requirements.in /tmp/requirements.in
RUN uv pip install --system torch --index-url https://download.pytorch.org/whl/cpu
RUN uv pip install --system -r /tmp/requirements.in \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

FROM ndif_base

ARG NAME
COPY src/services/${NAME}/requirements.in /tmp/requirements.in
RUN uv pip install --system -r /tmp/requirements.in \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /tmp/* \
    && rm -rf /root/.cache

# --mount=type=bind` temporarily mounts content into a `RUN` command
# **without adding it as a layer** to the image.
# cp -L copies symlinks in: *dereferences* them so they are copied
RUN --mount=type=bind,source=/src,target=/tmp/src \
    cp -rL /tmp/src/services/${NAME}/src / && \
    cp /tmp/src/services/${NAME}/start.sh ./start.sh

SHELL ["/bin/bash", "-c"]
CMD bash ./start.sh
