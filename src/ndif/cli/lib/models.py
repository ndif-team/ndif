"""Model-key resolution and controller state helpers for the model-op commands.

Ray and nnsight are imported lazily inside the functions so the bare CLI (help,
service lifecycle) stays fast and importable without the heavy compute deps.
"""

from __future__ import annotations

import time

# Default nnsight wrapper class; its import path prefixes the model_key so the
# server knows which class to reconstruct.
DEFAULT_ENVOY_CLASS = "nnsight.modeling.transformers.TransformersModel"


def get_model_key(checkpoint: str, revision: str | None = None,
                  envoy_class: str | None = None) -> str:
    """Resolve the canonical model_key for a checkpoint via nnsight.

    ``envoy_class`` selects the nnsight wrapper (defaults to ``TransformersModel``).
    Construction is meta-lazy (no weights loaded); the lookup canonicalises the
    HuggingFace repo id via the Hub.
    """
    from nnsight.util import from_import_path

    cls = from_import_path(envoy_class or DEFAULT_ENVOY_CLASS)
    return cls(checkpoint, revision=revision).to_model_key()


def extract_repo_id_from_model_key(model_key: str) -> str:
    """Pull the ``repo_id`` out of a model_key, or return it unchanged."""
    # model_key format: 'nnsight.modeling.language.LanguageModel:{"repo_id": "...", ...}'
    try:
        if '"repo_id":' in model_key:
            start = model_key.index('"repo_id":') + len('"repo_id":')
            remainder = model_key[start:].strip()
            if remainder.startswith('"'):
                end = remainder.index('"', 1)
                return remainder[1:end]
    except (ValueError, IndexError):
        pass
    return model_key


def canonicalize_checkpoint(checkpoint: str, revision: str | None = None,
                            envoy_class: str | None = None) -> tuple[str, str | None, str]:
    """Resolve a user-typed checkpoint to ``(canonical_repo_id, revision, model_key)``.

    One nnsight lookup yields both the Hub-canonical repo id and the model_key,
    so callers (e.g. the dashboard's schedule store) can persist both without
    paying the resolution cost twice.
    """
    model_key = get_model_key(checkpoint, revision, envoy_class)
    return extract_repo_id_from_model_key(model_key), revision, model_key


def get_current_deployments(level: str | None = "HOT") -> list[dict]:
    """Deployment entries from ``controller.status()``, filtered by ``level``.

    Requires Ray to be connected first. ``level`` is one of HOT/WARM/COLD, or
    None for all. Each entry is a per-replica dict (repo_id, revision, pinned,
    model_key, replica_id, ...).
    """
    import ray

    from ...common.providers.ray import get_controller_actor_handle

    controller = get_controller_actor_handle()
    status = ray.get(controller.status.remote())
    deployments = status.get("deployments", {})

    if level:
        return [d for d in deployments.values() if d.get("deployment_level") == level]
    return list(deployments.values())


def wait_for_replica_ready(model_key: str, replica_id: str, timeout: int = 300) -> bool:
    """Poll a replica's actor ``__ray_ready__`` until ready or ``timeout``.

    Transient states are tolerated (keep polling): ``ValueError`` (actor not
    registered yet) and ``RayActorError`` (actor mid-respawn under
    ``max_restarts=-1``).
    """
    import ray
    import ray.exceptions

    from ...common.providers.ray import get_model_actor_handle

    start = time.time()
    while time.time() - start < timeout:
        try:
            handle = get_model_actor_handle(model_key, replica_id)
            ray.get(handle.__ray_ready__.remote())
            return True
        except (ray.exceptions.RayActorError, ValueError):
            time.sleep(2)
    return False
