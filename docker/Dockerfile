FROM astral/uv:python3.12-trixie-slim

# Sometimes apt-get tries to open a dialog box, this stops it
ENV DEBIAN_FRONTEND=noninteractive

# Install base utilities in single layer with cleanup
RUN apt-get update -y \
    && apt-get install -y build-essential git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ARG NAME
ENV NDIF_SERVICE=${NAME}

# Copy the entire repo
COPY . /ndif
WORKDIR /ndif

# Install service requirements and the ndif CLI
RUN uv pip install --system -r src/services/${NAME}/requirements.in \
    && pip install . \
    && find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete \
    && find /usr/local/lib/python3.12 -name "*.pyo" -delete \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "test" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.a" -delete \
    && rm -rf /root/.cache

SHELL ["/bin/bash", "-c"]
CMD ndif start ${NDIF_SERVICE} --verbose