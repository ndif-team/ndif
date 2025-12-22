import os
from pathlib import Path
import ray

def get_repo_root() -> Path:
    """Get the repository root directory"""
    current_file = Path(__file__).resolve()
    # Go up from ndif/commands/start.py -> ndif/ -> repo_root/
    return current_file.parent.parent.parent

def get_pid_dir() -> Path:
    """Get directory for storing PIDs"""
    pid_dir = Path.home() / ".ndif" / "pids"
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
