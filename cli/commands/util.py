import os
import pickle
import time
from pathlib import Path
import ray
import redis.asyncio as redis
from typing import Optional
import requests


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


def get_default_revision(checkpoint: str) -> str:
    """Get the default revision/branch for a model from HuggingFace.
    
    Args:
        checkpoint: Model checkpoint ID (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
        
    Returns:
        Default revision name (e.g., "main", "master")
    """
    try:
        # Query HuggingFace model info API
        url = f"https://huggingface.co/api/models/{checkpoint}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        
        model_info = response.json()
        # Default branch is stored in 'sha' field for the default branch
        # but we need to check the 'siblings' for branch info
        if "siblings" in model_info:
            # Find the default branch - usually listed first or marked as main/master
            for sibling in model_info["siblings"]:
                if sibling.get("rfilename") == ".gitattributes":
                    # This indicates the repo structure; default is usually main
                    return "main"
        
        # Fallback to common defaults
        return "main"
    except Exception:
        # If API call fails, default to "main" as it's the most common default
        return "main"


def get_model_key(checkpoint: str, revision: Optional[str] = None) -> str:
    """Generate a model key for a checkpoint and revision.
    
    Args:
        checkpoint: Model checkpoint ID (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
        revision: Model revision/branch. If None, uses the actual default from HuggingFace.
        
    Returns:
        Model key for use with NDIF
    """
    # TODO: This is a temporary workaround to get the model key. There should be a more lightweight way to do this.
    from nnsight import LanguageModel
    
    # If no revision specified, get the actual default from HuggingFace
    if revision is None:
        revision = get_default_revision(checkpoint)
    
    model = LanguageModel(checkpoint, revision=revision, dispatch=False)
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
