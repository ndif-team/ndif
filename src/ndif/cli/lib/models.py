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
                  envoy_class: str | None = None, task: str | None = None) -> str:
    """Resolve the canonical model_key for a checkpoint via nnsight.

    ``envoy_class`` selects the nnsight wrapper (defaults to ``TransformersModel``).
    ``task`` pins the pipeline task; left unset it is inferred from the
    checkpoint, exactly as an nnsight client with no ``task=`` does, so the two
    mint the same key. Construction is meta-lazy (no weights loaded); the lookup
    canonicalises the HuggingFace repo id via the Hub.
    """
    from nnsight.util import from_import_path

    cls = from_import_path(envoy_class or DEFAULT_ENVOY_CLASS)
    kwargs = {} if task is None else {"task": task}
    return cls(checkpoint, revision=revision, **kwargs).to_model_key()


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


def extract_task_from_model_key(model_key: str) -> str | None:
    """Pull the pipeline ``task`` out of a model_key.

    None for a suffix that carries no task — a non-JSON suffix (e.g. a VLLM
    key), or a wrapper whose identity has no task.
    """
    import json

    try:
        return json.loads(model_key.split(":", 1)[1]).get("task")
    except (IndexError, ValueError, AttributeError):
        return None


def canonicalize_checkpoint(checkpoint: str, revision: str | None = None,
                            envoy_class: str | None = None,
                            task: str | None = None) -> tuple[str, str | None, str]:
    """Resolve a user-typed checkpoint to ``(canonical_repo_id, revision, model_key)``.

    One nnsight lookup yields both the Hub-canonical repo id and the model_key,
    so callers (e.g. the dashboard's schedule store) can persist both without
    paying the resolution cost twice.
    """
    model_key = get_model_key(checkpoint, revision, envoy_class, task)
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


def wait_for_replica_ready(model_key: str, replica_id: str) -> None:
    """Block until a replica's actor is ready to serve.

    Two failures mean "not yet" and are polled through:

      - ``ValueError``: the controller creates the actor asynchronously after
        ``deploy`` returns, so the lookup hasn't resolved yet.
      - ``ActorUnavailableError``: the actor exists but is restarting.

    **Anything else propagates**, which is the whole point. ``ActorDiedError``
    is what a constructor that raised looks like from here, and it carries that
    exception's traceback — so letting it out is how the caller learns a model
    refused to load rather than merely being slow. Catching its parent
    ``RayActorError`` instead swallows it, and since ``max_restarts=-1`` respawns
    the actor to raise the identical error again, a permanent failure then looks
    exactly like a slow start forever.

    That is why there is no timeout. A deadline here cannot tell the two apart
    either: it reports "timed out" for both, which is right for neither — a
    genuinely slow load is cut off, and a deterministic failure is described by
    how long it was polled rather than by what went wrong. Loading a large model
    across many GPUs has no sensible upper bound; a failure has a cause, and now
    that the cause escapes, waiting indefinitely is only waiting for something
    that is actually going to happen.
    """
    import ray
    from ray.exceptions import ActorUnavailableError

    from ...common.providers.ray import get_model_actor_handle

    while True:
        try:
            handle = get_model_actor_handle(model_key, replica_id)
            ray.get(handle.__ray_ready__.remote())
            return
        except (ActorUnavailableError, ValueError):
            time.sleep(2)
