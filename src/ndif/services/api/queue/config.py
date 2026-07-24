"""Configuration for the queue subsystem.

A frozen, typed snapshot loaded once from the environment at import. Replaces
the old mutable ``QueueConfig`` class attributes; Redis connection lives in
``common.providers.redis`` (NDIF_REDIS_URL), so it isn't repeated here.
"""

import os
from dataclasses import dataclass


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name}: {raw!r} is not a valid integer")
    if value <= 0:
        raise ValueError(f"{name}: {value} must be a positive integer")
    return value


@dataclass(frozen=True)
class QueueConfig:
    """Centralized queue/autoscaling settings.

    Attributes:
        queue_key: Redis list the API pushes requests onto and the dispatcher
            pops from. (NDIF_QUEUE_KEY)
        fetch_timeout_s: Blocking-pop timeout; bounds how long the dispatch
            loop waits when idle before re-checking evictions/errors.
            (NDIF_QUEUE_FETCH_TIMEOUT_S)
        fetch_batch_max: Max requests drained per dispatch iteration, to
            amortize Redis round-trips under load. (NDIF_QUEUE_FETCH_BATCH_MAX)
        autoscaling_interval_s: How often a Processor checks its queue head.
            (NDIF_AUTOSCALING_INTERVAL_S)
        autoscaling_wait_threshold_s: Scale up when the oldest queued request
            has waited longer than this. (NDIF_AUTOSCALING_WAIT_THRESHOLD_S)
        autoscaling_backoff_s: Pause after a scale-up so the new replica can
            drain pressure before another fires. (NDIF_AUTOSCALING_BACKOFF_S)
        autoscaling_max_replicas: Upper bound on replicas per model_key via
            autoscaling. (NDIF_AUTOSCALING_MAX_REPLICAS)
    """

    queue_key: str
    fetch_timeout_s: int
    fetch_batch_max: int
    autoscaling_interval_s: int
    autoscaling_wait_threshold_s: int
    autoscaling_backoff_s: int
    autoscaling_max_replicas: int

    @classmethod
    def from_env(cls) -> "QueueConfig":
        return cls(
            queue_key=os.environ.get("NDIF_QUEUE_KEY", "queue"),
            fetch_timeout_s=_positive_int("NDIF_QUEUE_FETCH_TIMEOUT_S", 10),
            fetch_batch_max=_positive_int("NDIF_QUEUE_FETCH_BATCH_MAX", 32),
            autoscaling_interval_s=_positive_int("NDIF_AUTOSCALING_INTERVAL_S", 5),
            autoscaling_wait_threshold_s=_positive_int(
                "NDIF_AUTOSCALING_WAIT_THRESHOLD_S", 30
            ),
            autoscaling_backoff_s=_positive_int("NDIF_AUTOSCALING_BACKOFF_S", 120),
            autoscaling_max_replicas=_positive_int(
                "NDIF_AUTOSCALING_MAX_REPLICAS", 3
            ),
        )


CONFIG = QueueConfig.from_env()
