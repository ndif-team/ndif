#!/bin/bash
#
# Start a Ray node for NDIF and (on the head) launch the controller.
#
# Head vs worker is chosen by NDIF_RAY_HEAD_ADDRESS: unset -> head; set (to the
# head's HOST:PORT) -> join that head as a worker. This is deliberately separate
# from NDIF_RAY_ADDRESS, which is the ray:// *client* address services connect
# with — it says nothing about whether this node is a head or a worker. Every
# ray-start flag is env-driven (defaults shown). Custom node resources
# (cuda_memory_bytes, cpu_memory_bytes, head=10) are computed by resources.py
# and read back by the controller.
set -euo pipefail

# Telemetry: label everything this service emits to Loki as `service=ray`.
# Exported before `ray start` so the raylet — and every actor process it later
# spawns (controller, model actors) — inherits it.
export NDIF_SERVICE="${NDIF_SERVICE:-ray}"

# Every `ray://` client connection (the API's dispatcher, `ndif status`, the
# dashboard) makes the Ray client proxier fork a per-client server. With
# grpc's fork support on (its default here), the proxier's gRPC threads make
# it skip its fork handlers and the child dies within 100 ms, silently, about
# half the time in a container and one time in six on bare metal — the client
# then waits 40 s for "Starting Ray client server failed". Turning fork
# support off removes the failure entirely (0/8 vs 9/17 failures, Ray 2.55.1,
# grpcio 1.84.0). Set before `ray start` so the proxier inherits it; an
# operator who sets it explicitly wins.
export GRPC_ENABLE_FORK_SUPPORT="${GRPC_ENABLE_FORK_SUPPORT:-0}"

NDIF_RAY_TEMP_DIR="${NDIF_RAY_TEMP_DIR:-/tmp/ray}"
mkdir -p "$NDIF_RAY_TEMP_DIR"
if [ ! -w "$NDIF_RAY_TEMP_DIR" ]; then
    echo "ERROR: Cannot write to Ray temp directory: $NDIF_RAY_TEMP_DIR" >&2
    echo "Set NDIF_RAY_TEMP_DIR to a writable location." >&2
    exit 1
fi
# Ray puts unix sockets under <temp>/session_<timestamp>_<pid>/sockets/, and
# AF_UNIX paths cannot exceed 107 bytes; the session part alone is ~64. A long
# temp dir makes `ray start` die at once with "validate_socket_filename
# failed", which is easy to mistake for a slow boot. Refuse it up front.
if [ "${#NDIF_RAY_TEMP_DIR}" -gt 40 ]; then
    echo "ERROR: NDIF_RAY_TEMP_DIR is ${#NDIF_RAY_TEMP_DIR} characters; Ray's socket paths" >&2
    echo "under it would exceed the 107-byte AF_UNIX limit. Use a short path, e.g. /tmp/ndif-ray." >&2
    exit 1
fi

HEAD_ADDRESS="${NDIF_RAY_HEAD_ADDRESS:-}"

# Block until host:port accepts a TCP connection (or give up after N tries).
wait_for_head() {
    local host="${1%%:*}"
    local port="${1##*:}"
    local retries="${NDIF_RAY_HEAD_WAIT_RETRIES:-60}"
    local interval="${NDIF_RAY_HEAD_WAIT_INTERVAL_S:-2}"
    local attempt=0

    echo "Waiting for Ray head at $host:$port..."
    until (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; do
        attempt=$((attempt + 1))
        if [ "$attempt" -ge "$retries" ]; then
            echo "ERROR: Ray head $host:$port not reachable after $attempt attempts" >&2
            exit 1
        fi
        sleep "$interval"
    done
    exec 3>&- || true
    echo "Ray head is reachable."
}

if [ -z "$HEAD_ADDRESS" ]; then
    # ---- Head node ----
    # Clear any ray-native RAY_ADDRESS so `ray start --head` doesn't try to
    # attach to an existing cluster instead of starting one.
    unset RAY_ADDRESS
    resources=$(python -m ndif.services.ray.resources --head)
    echo "Starting Ray head node with resources: $resources"

    ray start --head \
        --resources="$resources" \
        --port="${NDIF_RAY_HEAD_PORT:-6385}" \
        --object-manager-port="${NDIF_RAY_OBJECT_MANAGER_PORT:-8076}" \
        --include-dashboard=true \
        --dashboard-host=0.0.0.0 \
        --dashboard-port="${NDIF_RAY_DASHBOARD_PORT:-8265}" \
        --dashboard-agent-grpc-port="${NDIF_RAY_DASHBOARD_GRPC_PORT:-52366}" \
        --metrics-export-port="${NDIF_RAY_METRICS_PORT:-8080}" \
        --temp-dir="$NDIF_RAY_TEMP_DIR"

    echo "Starting NDIF controller..."
    python -m ndif.services.ray.deployments.controller.controller
else
    # ---- Worker node ----
    # ray start --address wants the head's HOST:PORT; tolerate a ray:// prefix.
    HEAD_ADDRESS="${HEAD_ADDRESS#ray://}"

    # Tolerate starting before the head is up (cluster boot ordering).
    wait_for_head "$HEAD_ADDRESS"

    resources=$(python -m ndif.services.ray.resources)
    echo "Starting Ray worker, joining $HEAD_ADDRESS, resources: $resources"

    ray start \
        --address="$HEAD_ADDRESS" \
        --resources="$resources" \
        --temp-dir="$NDIF_RAY_TEMP_DIR"
fi

# Keep the container in the foreground (ray start daemonizes).
tail -f /dev/null
