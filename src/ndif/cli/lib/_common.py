"""Shared helpers for the model-op lib modules (``deploy``, ``evict``, ``restart``).

Private to ``cli/lib/`` — callers import from the specific command module
(e.g. ``from ndif.cli.lib.deploy import deploy``).
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

OnMessage = Optional[Callable[[str], None]]


class NDIFConnectivityError(RuntimeError):
    """Raised when the Ray control plane is unreachable."""


def emit(on_message: OnMessage, msg: str) -> None:
    if on_message is not None:
        on_message(msg)


def ensure_ray_connected(ray_address: Optional[str] = None) -> None:
    """Ensure the Ray client is connected. Reset + reconnect once on failure.

    Liveness is delegated to ``RayProvider.connected()`` — which covers
    ``ray.is_initialized()``, the TCP listen probe, and a controller-handle
    round-trip.
    """
    from ...common.providers.ray import RayProvider

    if ray_address:
        RayProvider.ray_url = ray_address

    try:
        if RayProvider.connected():
            return
    except Exception:
        pass

    RayProvider.reset()
    try:
        RayProvider.connect()
    except Exception as e:
        raise NDIFConnectivityError(
            f"Cannot connect to Ray at {RayProvider.ray_url}: {e}"
        ) from e


def normalize_specs(specs: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for spec in specs:
        if "checkpoint" not in spec:
            raise ValueError(f"Model spec missing 'checkpoint': {spec}")
        out.append(
            {
                "checkpoint": spec["checkpoint"],
                "revision": spec.get("revision"),
                "pinned": bool(spec.get("pinned", False)),
                "replicas": int(spec.get("replicas") or 1),
                "actor_class": spec.get("actor_class"),
                "envoy_class": spec.get("envoy_class"),
                # Pipeline task baked into the model key (part of the model's
                # identity). Unset, nnsight infers it from the checkpoint, the
                # same way a client with no task= does.
                "task": spec.get("task"),
                # Whether the checkpoint's own modelling code may run at load.
                # Part of the model key, like the task: a client that loads with
                # it mints a different key, so it has to be said here for the
                # deployment to be the one that client reaches.
                "trust_remote_code": bool(spec.get("trust_remote_code", False)),
                "padding_factor": spec.get("padding_factor"),
                "size_bytes": spec.get("size_bytes"),
                "padding_bias": spec.get("padding_bias"),
                "gpus": spec.get("gpus"),
                "max_tp": spec.get("max_tp"),
                "execution_timeout_seconds": spec.get("execution_timeout_seconds"),
                # Optional pre-computed canonical key — when set, deploy skips
                # ``get_model_key`` and uses this value verbatim.
                "model_key": spec.get("model_key"),
            }
        )
    return out
