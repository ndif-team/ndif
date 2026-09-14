"""``ndif scale`` — add replicas that look like the ones already running.

The difference from ``deploy`` is only where the unspecified fields come from.
``deploy`` fills them from the controller's defaults, which is what you want when
you are saying how a model should be served. ``scale`` fills them from a replica
already serving that model, which is what you want when you are saying *more of
that* — growing a tensor-parallel model should give you more tensor-parallel
replicas, not a second opinion about how the model is served.
"""

from __future__ import annotations

from typing import Optional

from .. import config
from ._common import OnMessage, emit, ensure_ray_connected
from .models import get_model_key


def scale(
    checkpoint: Optional[str] = None,
    *,
    n: int = 1,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    actor_class: Optional[str] = None,
    dtype: Optional[str] = None,
    gpus: Optional[int] = None,
    execution_timeout_seconds: Optional[float] = None,
    trusted: bool = False,
    pinned: bool = False,
    ray_address: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Add ``n`` replicas of a model, matching what is already deployed.

    ``n`` is additive — how many to add, not a target — like
    ``deploy --replicas``. Any field given here is used as-is and also decides
    which live replica counts as a match to copy the rest from; anything left out
    is taken from that replica. With none running there is nothing to copy and
    this behaves as a plain deploy.

    Returns ``{"model_key", "replicas": [...], "error": ...}``.
    """
    import ray

    from ...common.providers.ray import get_controller_actor_handle
    from ...common.schema.controller import DeploymentConfig

    if (checkpoint is None) == (model_key is None):
        raise ValueError("Specify exactly one of: checkpoint, model_key")
    if n < 1:
        raise ValueError("n must be at least 1 — scale is additive; use evict to remove")

    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")
    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)
    controller = get_controller_actor_handle()

    if model_key is None:
        model_key = get_model_key(checkpoint, revision)
        emit(on_message, f"  Model key for {checkpoint}: {model_key}")

    requested = DeploymentConfig(
        trusted=trusted,
        pinned=pinned,
        actor_class=actor_class,
        dtype=dtype,
        gpus=gpus,
        execution_timeout_seconds=execution_timeout_seconds,
    )

    emit(on_message, f"Adding {n} replica(s) of {model_key}...")
    response = ray.get(controller.scale.remote(model_key, n, requested))

    result = response.results.get(model_key)
    replicas = list(result.replicas) if result else []
    error = result.error if result else None

    if error:
        emit(on_message, f"  ✗ {model_key}: {error}")
    else:
        for replica_id in replicas:
            emit(on_message, f"  ✓ {model_key} [{replica_id}]: added")

    return {"model_key": model_key, "replicas": replicas, "error": error}
