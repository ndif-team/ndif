import os
from typing import Dict, Optional

from . import Metric


class DeploymentStateMetric(Metric):
    """One point per live model replica (HOT or WARM).

    Captures the application-level view of *what* is deployed, *where*, and
    its sizing metadata. Emitted by the Controller on a periodic heartbeat and
    immediately after every ``apply()``, so for any timestamp you can recover
    the deployment state that was active and join it against node/GPU profiles.

    Tags are the (low-cardinality) series identity — model, replica, location,
    level. Fields carry the sizing numbers (base vs padded VRAM, params, age).
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

    Purpose-built to be joined against Ray's per-GPU profiles (which are
    labelled by node IP + GPU index): given a timestamp and a (node_ip,
    gpu_index), this tells you which model occupies that GPU and how many
    bytes the controller *planned* to allocate (base model + padding) — the
    counterpart to the *actual* used VRAM from the GPU profile.

    ``gpu_available_memory_bytes`` is the controller's accounting view of free
    space on that GPU, not a live measurement.
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
        gpu_total_memory_bytes: int,
        gpu_available_memory_bytes: int,
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
            .field("gpu_total_memory_bytes", int(gpu_total_memory_bytes))
            .field("gpu_available_memory_bytes", int(gpu_available_memory_bytes))
        )

        super().update(point)
