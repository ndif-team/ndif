"""Capture system and service metadata."""

import os
import platform
import psutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def get_container_id() -> Optional[str]:
    """Get the Docker container ID if running in a container.

    Returns:
        Container ID string, or None if not in a container.
    """
    try:
        # Try to read from cgroup file
        cgroup_path = Path("/proc/self/cgroup")
        if cgroup_path.exists():
            with open(cgroup_path) as f:
                for line in f:
                    if "docker" in line or "containerd" in line:
                        # Extract container ID from cgroup path
                        parts = line.strip().split("/")
                        if parts:
                            container_id = parts[-1]
                            # Remove any .scope suffix
                            if container_id.endswith(".scope"):
                                container_id = container_id[:-6]
                            # Return first 12 chars (standard short container ID)
                            return container_id[:12] if len(container_id) > 12 else container_id
        return None
    except Exception:
        return None


def get_service_metadata(service_name: str) -> dict:
    """Get service identification and lifecycle metadata.

    Args:
        service_name: Name of the service (e.g., "api", "ray")

    Returns:
        Dictionary with service metadata.
    """
    # Get process start time
    process = psutil.Process(os.getpid())
    start_time = datetime.fromtimestamp(process.create_time(), tz=timezone.utc)

    return {
        "name": service_name,
        "container_id": get_container_id(),
        "start_time": start_time.isoformat(),
        "pid": os.getpid(),
    }


def get_system_metadata() -> dict:
    """Get static system/platform metadata.

    Returns:
        Dictionary with system metadata.
    """
    # Get memory info
    memory = psutil.virtual_memory()

    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "total_memory_bytes": memory.total,
        "total_memory_gb": round(memory.total / (1024**3), 2),
    }