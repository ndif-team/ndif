"""Utility functions for NDIF CLI"""

import time
from pathlib import Path

import ray
import redis.asyncio as redis


# ASCII art for NDIF logo
NDIF_LOGO = [
    "                              ",
    " ███╗   ██╗██████╗ ██╗███████╗",
    " ████╗  ██║██╔══██╗██║██╔════╝",
    " ██╔██╗ ██║██║  ██║██║█████╗  ",
    " ██║╚██╗██║██║  ██║██║██╔══╝  ",
    " ██║ ╚████║██████╔╝██║██║     ",
    " ╚═╝  ╚═══╝╚═════╝ ╚═╝╚═╝     ",
]


def print_logo():
    """Print the NDIF logo with a purple to light blue gradient."""
    start_color = (148, 0, 211)  # Purple
    end_color = (135, 206, 250)  # Light blue

    width = len(NDIF_LOGO[0])

    for line in NDIF_LOGO:
        colored_line = ""
        for i, char in enumerate(line):
            factor = i / (width - 1) if width > 1 else 0
            r = int(start_color[0] + (end_color[0] - start_color[0]) * factor)
            g = int(start_color[1] + (end_color[1] - start_color[1]) * factor)
            b = int(start_color[2] + (end_color[2] - start_color[2]) * factor)
            colored_line += f"\033[38;2;{r};{g};{b}m{char}\033[0m"
        print(colored_line)
    print()


def get_ndif_root() -> Path:
    """Get the root of the installed ndif package directory.

    Works identically in development (editable install) and installed modes —
    resolves to wherever the `ndif` package was imported from, e.g.
    ``<repo>/src/ndif`` or ``site-packages/ndif``.
    """
    import ndif
    return Path(ndif.__file__).resolve().parent


def get_service_dir(service_name: str) -> Path:
    """Get the filesystem directory for a given service (api, ray, monitor)."""
    return get_ndif_root() / "services" / service_name


# =============================================================================
# Ray utilities
# =============================================================================


def get_controller_actor_handle(namespace: str = "NDIF") -> ray.actor.ActorHandle:
    """Get a Ray actor handle for the controller actor."""
    return ray.get_actor("Controller", namespace=namespace)


def get_actor_handle(model_key: str, namespace: str = "NDIF") -> ray.actor.ActorHandle:
    """Get a Ray actor handle by model key and namespace.

    Args:
        model_key: Model key
        namespace: Ray namespace (default: "NDIF")

    Returns:
        Ray actor handle
    """
    return ray.get_actor(f"ModelActor:{model_key}", namespace=namespace)


def get_model_key(checkpoint: str, revision: str = None) -> str:
    """Get the model key for a checkpoint.

    Args:
        checkpoint: Model checkpoint/repo ID
        revision: Model revision (default: None, uses model's default)

    Returns:
        Model key string
    """
    # TODO: This is a temporary workaround to get the model key.
    # There should be a more lightweight way to do this.
    from nnsight import LanguageModel

    model = LanguageModel(checkpoint, revision=revision, dispatch=False)
    return model.to_model_key()


def extract_repo_id_from_model_key(model_key: str) -> str:
    """Extract repo_id from model_key string.

    Args:
        model_key: Full model key string

    Returns:
        The repo_id if found, otherwise the original model_key
    """
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


async def notify_dispatcher(redis_url: str, event_type: str, model_key: str):
    """Notify dispatcher of deployment changes via Redis streams.

    Args:
        redis_url: Redis connection URL
        event_type: Type of event ("deploy" or "evict")
        model_key: Model key affected by the event
    """
    redis_client = redis.Redis.from_url(redis_url)
    try:
        await redis_client.xadd(
            "dispatcher:events",
            {
                "event_type": event_type,
                "model_key": model_key,
                "timestamp": str(time.time()),
            }
        )
    finally:
        await redis_client.aclose()


def get_current_deployments(level: str = "HOT") -> list[dict]:
    """Fetch current deployments from the controller.

    Requires Ray to be initialized first.

    Args:
        level: Deployment level to filter by ("HOT", "WARM", "COLD", or None for all)

    Returns:
        List of deployment dicts with repo_id, revision, dedicated, model_key, etc.
    """
    controller = get_controller_actor_handle()
    status_ref = controller.status.remote()
    status = ray.get(status_ref)

    deployments = status.get("deployments", {})

    if level:
        return [
            dep for dep in deployments.values()
            if dep.get("deployment_level") == level
        ]
    return list(deployments.values())


def wait_for_model_ready(model_key: str, timeout: int = 300) -> bool:
    """Wait for a model actor to be ready.

    Polls the model actor's __ray_ready__ method until it returns successfully,
    indicating the model is fully loaded and ready for inference.

    Args:
        model_key: The model key to wait for
        timeout: Maximum seconds to wait (default: 300 = 5 minutes)

    Returns:
        True if model is ready, False if timeout or error

    Raises:
        Exception: If an initialization error occurs (not a lookup failure)
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            handle = get_actor_handle(model_key)
            ray.get(handle.__ray_ready__.remote())
            return True
        except Exception as e:
            error_str = str(e)
            # Actor doesn't exist yet - keep waiting
            if "Failed to look up actor" in error_str:
                time.sleep(2)
                continue
            # Actual initialization error
            raise

    return False
