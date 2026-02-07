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


def get_repo_root() -> Path:
    """Get the repository root directory.

    Works in both development (repo) and installed (site-packages) modes.
    Finds the parent directory containing both 'cli' and 'src'.
    """
    current_file = Path(__file__).resolve()

    for parent in [current_file.parent] + list(current_file.parents):
        if (parent / "cli").exists() and (parent / "src").exists():
            return parent

    raise RuntimeError(
        "Could not find NDIF package root. "
        "Expected to find a directory containing both 'cli' and 'src' subdirectories."
    )


# =============================================================================
# Ray utilities
# =============================================================================


def get_controller_actor_handle(namespace: str = "NDIF") -> ray.actor.ActorHandle:
    """Get a Ray actor handle for the controller actor."""
    return ray.get_actor("Controller", namespace=namespace)


def get_actor_handle(model_key: str, namespace: str = "NDIF", replica_id: int = 0) -> ray.actor.ActorHandle:
    """Get a Ray actor handle by model key and namespace.

    Args:
        model_key: Model key
        namespace: Ray namespace (default: "NDIF")
        replica_id: Replica ID (default: 0)
    Returns:
        Ray actor handle
    """
    return ray.get_actor(model_actor_name(model_key, replica_id), namespace=namespace)

def model_actor_name(model_key: str, replica_id: int = 0) -> str:
    return f"ModelActor:{model_key}:{replica_id}"


def get_model_key(checkpoint: str, revision: str = "main") -> str:
    """Get the model key for a checkpoint.

    Args:
        checkpoint: Model checkpoint/repo ID
        revision: Model revision (default: "main")

    Returns:
        Model key string
    """
    # TODO: This is a temporary workaround to get the model key.
    # There should be a more lightweight way to do this.
    from nnsight import LanguageModel

    model = LanguageModel(checkpoint, revision=None, dispatch=False)
    return model.to_model_key()


async def notify_dispatcher(redis_url: str, event_type: str, model_key: str, replicas: int | None = None, replica_id: int | None = None):
    """Notify dispatcher of deployment changes via Redis streams.

    Args:
        redis_url: Redis connection URL
        event_type: Type of event ("deploy" or "evict")
        model_key: Model key affected by the event
    """
    redis_client = redis.Redis.from_url(redis_url)
    try:
        payload = {
            "event_type": event_type,
            "model_key": model_key,
            "timestamp": str(time.time()),
        }
        if replicas is not None:
            payload["replicas"] = replicas
        if replica_id is not None:
            payload["replica_id"] = replica_id
        await redis_client.xadd(
            "dispatcher:events",
            payload,
        )
    finally:
        await redis_client.aclose()


