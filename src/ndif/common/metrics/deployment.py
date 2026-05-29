import os
from typing import Dict, Optional

from . import Metric


class DeploymentStateMetric(Metric):
    """One point per live model replica (HOT or WARM).

    Captures the application-level view of *what* is deployed, *where*, and its
    sizing metadata. Emitted by the Controller on a periodic heartbeat and
    immediately after every ``apply()``, so for any timestamp you can recover
    the deployment state that was active and join it against node/GPU profiles.

    Liveness is implicit: a deployment is "alive" iff a point was emitted within
    the last heartbeat interval. The controller drops dead/evicted/failed
    deployments from its state, so they simply stop being emitted — current-
    state queries should use a short lookback window rather than a wide range.

    Tags are the (low-cardinality) series identity; fields carry the sizing
    numbers (base vs padded VRAM, params, age).
    """

    name: str = "deployment_state"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        replica_id: str,
        node_id: str,
        node_name: str,
        node_ip: Optional[str],
        deployment_level: str,
        pinned: bool,
        actor_class: Optional[str],
        base_size_bytes: int,
        padded_size_bytes: int,
        n_params: int,
        gpus: Dict[int, int],
        age_seconds: float,
    ) -> None:
        # Short-circuit before building the Point if InfluxDB isn't configured
        # (the base class also no-ops, but Point isn't importable then).
        if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is None:
            return

        from influxdb_client import Point

        gpu_indices = ",".join(str(i) for i in sorted(gpus))

        point = (
            Point(cls.name)
            .tag("model_key", model_key)
            .tag("replica_id", replica_id)
            .tag("node_id", node_id)
            .tag("node_name", node_name)
            .tag("node_ip", node_ip or "")
            .tag("deployment_level", deployment_level)
            .tag("pinned", str(pinned).lower())
            .tag("actor_class", actor_class or "")
            .field("base_size_bytes", int(base_size_bytes))
            .field("padded_size_bytes", int(padded_size_bytes))
            .field("padding_bytes", int(padded_size_bytes - base_size_bytes))
            .field("n_params", int(n_params))
            .field("num_gpus", len(gpus))
            .field("gpu_indices", gpu_indices)
            .field("total_allocated_bytes", int(sum(gpus.values())))
            .field("age_seconds", float(age_seconds))
        )

        super().update(point)


class DeploymentGPUMetric(Metric):
    """One point per (replica, GPU) for GPU-resident (HOT) replicas.

    The model-attributed allocation: for a given (node_ip, gpu_index) at a
    point in time, which model/replica occupies that GPU and how many bytes the
    controller *planned* to reserve for it (base model + padding). This is the
    counterpart to the *actual* used VRAM from Ray's per-GPU profile (joined on
    node_ip + gpu_index). GPU-level totals/free live in ``node_gpu`` instead, so
    they aren't duplicated across co-located replicas.
    """

    name: str = "deployment_gpu"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        replica_id: str,
        node_id: str,
        node_name: str,
        node_ip: Optional[str],
        deployment_level: str,
        gpu_index: int,
        allocated_bytes: int,
    ) -> None:
        if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is None:
            return

        from influxdb_client import Point

        point = (
            Point(cls.name)
            .tag("model_key", model_key)
            .tag("replica_id", replica_id)
            .tag("node_id", node_id)
            .tag("node_name", node_name)
            .tag("node_ip", node_ip or "")
            .tag("deployment_level", deployment_level)
            .tag("gpu_index", str(gpu_index))
            .field("allocated_bytes", int(allocated_bytes))
        )

        super().update(point)


class NodeGPUMetric(Metric):
    """One point per (node, GPU) — the controller's GPU resource accounting.

    Emitted for every GPU on every node each snapshot, whether or not it holds
    a deployment, so idle GPUs still report full capacity. ``allocated_bytes``
    and ``available_memory_bytes`` are the controller's *planned* view (sum of
    replica reservations vs. free); on a shared machine they ignore other
    processes — that gap is exactly what joining against Ray's per-GPU profile
    (on node_ip + gpu_index) reveals. One clean series per GPU, so per-GPU
    panels don't double-count co-located replicas.
    """

    name: str = "node_gpu"

    @classmethod
    def update(
        cls,
        *,
        node_id: str,
        node_name: str,
        node_ip: Optional[str],
        gpu_type: str,
        gpu_index: int,
        total_memory_bytes: int,
        allocated_bytes: int,
        available_memory_bytes: int,
        num_replicas: int,
    ) -> None:
        if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is None:
            return

        from influxdb_client import Point

        point = (
            Point(cls.name)
            .tag("node_id", node_id)
            .tag("node_name", node_name)
            .tag("node_ip", node_ip or "")
            .tag("gpu_type", gpu_type or "")
            .tag("gpu_index", str(gpu_index))
            .field("total_memory_bytes", int(total_memory_bytes))
            .field("allocated_bytes", int(allocated_bytes))
            .field("available_memory_bytes", int(available_memory_bytes))
            .field("num_replicas", int(num_replicas))
        )

        super().update(point)
