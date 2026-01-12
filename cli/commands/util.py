import os
import pickle
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
    """Get the repository root directory

    Works in both development (repo) and installed (site-packages) modes.
    Finds the parent directory containing both 'cli' and 'src'.
    """
    current_file = Path(__file__).resolve()

    # Start from current file and walk up to find directory containing both 'cli' and 'src'
    # In dev mode: .../ndif/cli/commands/util.py -> .../ndif/
    # In installed mode: .../site-packages/cli/commands/util.py -> .../site-packages/
    for parent in [current_file.parent] + list(current_file.parents):
        if (parent / "cli").exists() and (parent / "src").exists():
            return parent

    # If not found, raise an error
    raise RuntimeError(
        "Could not find NDIF package root. "
        "Expected to find a directory containing both 'cli' and 'src' subdirectories."
    )


def get_pid_dir() -> Path:
    """Get directory for storing PIDs"""
    pid_dir = Path.home() / ".ndif" / "pids"
    pid_dir.mkdir(parents=True, exist_ok=True)
    return pid_dir


def get_pid(service: str) -> int:
    """Get saved PID for a service"""
    pid_file = get_pid_dir() / f"{service}.pid"
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def save_pid(service: str, pid: int):
    """Save a service PID to file"""
    pid_file = get_pid_dir() / f"{service}.pid"
    pid_file.write_text(str(pid))


def clear_pid(service: str):
    """Remove saved PID file"""
    pid_file = get_pid_dir() / f"{service}.pid"
    if pid_file.exists():
        pid_file.unlink()


def is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running"""
    try:
        os.kill(pid, 0)  # Signal 0 doesn't kill, just checks if process exists
        return True
    except (OSError, ProcessLookupError):
        return False


# Ray utilities


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


def get_model_key(checkpoint: str, revision: str = "main") -> str:

    # TODO: This is a temporary workaround to get the model key. There should be a more lightweight way to do this.
    from nnsight import LanguageModel

    model = LanguageModel(checkpoint, revision=None, dispatch=False)
    return model.to_model_key()


async def notify_dispatcher(redis_url: str, event_type: str, model_key: str):
    """Notify dispatcher of deployment changes via Redis.

    Args:
        redis_url: Redis connection URL
        event_type: Type of event ("deploy" or "evict")
        model_key: Model key affected by the event
    """
    redis_client = redis.Redis.from_url(redis_url)
    try:
        event = {"type": event_type, "model_key": model_key, "timestamp": time.time()}
        await redis_client.lpush("deployment_events", pickle.dumps(event))
    finally:
        await redis_client.aclose()
