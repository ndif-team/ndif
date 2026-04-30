"""Shared helpers used by the per-command lib modules (``deploy``, ``evict``,
``restart``, ``status``).

Anything in here is private to ``cli/lib/`` — callers should import from the
specific command module (e.g. ``from ndif.cli.lib.deploy import deploy``).
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from .checks import check_ray, check_redis


logger = logging.getLogger(__name__)

OnMessage = Optional[Callable[[str], None]]


class NDIFConnectivityError(RuntimeError):
    """Raised when prerequisite services (Redis broker / Ray) are unreachable."""


def emit(on_message: OnMessage, msg: str) -> None:
    if on_message is not None:
        on_message(msg)


def check_prereqs(broker_url: Optional[str], ray_address: Optional[str]) -> None:
    if broker_url and not check_redis(broker_url):
        raise NDIFConnectivityError(f"Cannot reach broker at {broker_url}")
    if ray_address and not check_ray(ray_address):
        raise NDIFConnectivityError(f"Cannot reach Ray at {ray_address}")


def ensure_ray_connected(ray_address: Optional[str]) -> None:
    """Make sure the Ray client is connected and the session is *alive*.

    ``ray.is_initialized()`` returns True even after the Ray server has
    cleaned up our session, in which case the next remote call dies with
    "Attempted to reconnect a session that has already been cleaned up".
    Long-running FastAPI processes hit this when they sit idle past the
    server's session timeout.

    To avoid that we actively probe with a cheap ``get_actor("Controller")``
    after init. If the probe trips one of ``RayProvider``'s known
    connection-error patterns, we reset the local Ray state and reconnect
    once.

    Raises:
        NDIFConnectivityError: if the broker port isn't even listening, or
            if the second connect attempt also fails.
    """
    import ray

    # Lazy import — avoids dragging the broker stack into nnsight CLI startup.
    from ...common.providers.ray import RayProvider

    if ray_address and not check_ray(ray_address):
        raise NDIFConnectivityError(f"Cannot reach Ray at {ray_address}")

    def _try_init() -> None:
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

    def _is_alive() -> bool:
        try:
            ray.get_actor("Controller", namespace="NDIF")
            return True
        except Exception as e:
            if RayProvider.is_connection_error(e):
                return False
            # Anything else (e.g. controller actor not yet up) — treat as alive
            # for our purposes; the caller will surface the real error.
            return True

    if ray.is_initialized() and _is_alive():
        return

    if ray.is_initialized():
        # Initialized but dead → reset before reconnecting; ray.init alone
        # won't refresh a session the server already cleaned up.
        logger.warning("Ray client session is dead; resetting before reconnect")
        try:
            RayProvider.reset()
        except Exception:
            pass

    try:
        _try_init()
    except Exception as e:
        if RayProvider.is_connection_error(e):
            logger.warning("Ray init hit connection error, resetting and retrying once: %s", e)
            try:
                RayProvider.reset()
            except Exception:
                pass
            try:
                _try_init()
            except Exception as e2:
                raise NDIFConnectivityError(
                    f"Cannot connect to Ray at {ray_address}: {e2}"
                ) from e2
        else:
            raise


def normalize_specs(specs: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for spec in specs:
        if "checkpoint" not in spec:
            raise ValueError(f"Model spec missing 'checkpoint': {spec}")
        out.append(
            {
                "checkpoint": spec["checkpoint"],
                "revision": spec.get("revision"),
                "dedicated": bool(spec.get("dedicated", False)),
                "actor_class": spec.get("actor_class"),
                "padding_factor": spec.get("padding_factor"),
                "execution_timeout_seconds": spec.get("execution_timeout_seconds"),
            }
        )
    return out
