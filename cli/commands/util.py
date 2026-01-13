import os
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


def get_log_dir() -> Path:
    """Get base directory for storing logs"""
    return Path("/tmp/ndif")


def get_session_log_dir(service: str) -> Path:
    """Create and return a new session log directory for a service

    Creates a timestamped session directory.
    Returns the path and ensures it exists.
    """
    from datetime import datetime

    base_dir = get_log_dir() / service
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    session_dir = base_dir / f"session_{timestamp}_{os.getpid()}"
    session_dir.mkdir(parents=True, exist_ok=True)

    # Create a 'latest' symlink pointing to this session
    latest_link = base_dir / "latest"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(session_dir)

    return session_dir


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
    """Notify dispatcher of deployment changes via Redis streams.

    Args:
        redis_url: Redis connection URL
        event_type: Type of event ("deploy" or "evict")
        model_key: Model key affected by the event
    """
    redis_client = redis.Redis.from_url(redis_url)
    try:
        # Send event to dispatcher events stream
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


def check_redis(redis_url: str, timeout: int = 2) -> bool:
    """Check if Redis is reachable.

    Args:
        redis_url: Redis connection URL
        timeout: Connection timeout in seconds

    Returns:
        True if Redis is reachable, False otherwise
    """
    try:
        import redis as redis_sync
        client = redis_sync.Redis.from_url(redis_url, socket_connect_timeout=timeout)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def check_minio(minio_url: str, timeout: int = 2) -> bool:
    """Check if MinIO is reachable.

    Args:
        minio_url: MinIO connection URL
        timeout: Connection timeout in seconds

    Returns:
        True if MinIO is reachable, False otherwise
    """
    try:
        import requests
        # Try to access the MinIO health endpoint
        response = requests.get(f"{minio_url}/minio/health/live", timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def check_api(api_url: str, timeout: int = 2) -> bool:
    """Check if NDIF API is reachable.

    Args:
        api_url: API URL to check
        timeout: Connection timeout in seconds

    Returns:
        True if API is reachable, False otherwise
    """
    try:
        import requests
        # Try to access the API ping endpoint
        response = requests.get(f"{api_url}/ping", timeout=timeout)
        return response.status_code == 200
    except Exception:
        return False


def check_ray(ray_address: str, timeout: int = 2) -> bool:
    """Check if Ray port is listening.

    Args:
        ray_address: Ray address (e.g., ray://localhost:10001)
        timeout: Connection timeout in seconds

    Returns:
        True if Ray port is listening, False otherwise
    """
    try:
        import socket
        from urllib.parse import urlparse

        # Parse the ray address to get host and port
        # ray://localhost:10001 -> localhost, 10001
        parsed = urlparse(ray_address)
        host = parsed.hostname or 'localhost'
        port = parsed.port or 10001

        # Try to connect to the port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def check_prerequisites(redis_url: str = None, minio_url: str = None,
                       api_url: str = None, ray_address: str = None,
                       verbose: bool = False):
    """Check prerequisites and exit if any are unreachable.

    Args:
        redis_url: Redis URL to check (optional)
        minio_url: MinIO URL to check (optional)
        api_url: API URL to check (optional)
        ray_address: Ray address to check (optional)
        verbose: If True, show checking messages and success. If False, only show errors.
    """
    import click
    import sys

    if verbose:
        click.echo("Checking prerequisites...")

    if redis_url:
        redis_reachable = check_redis(redis_url)
        if not redis_reachable:
            click.echo(f"✗ Error: Cannot reach Redis at {redis_url}", err=True)
            click.echo("  Make sure Redis is running and accessible.", err=True)
            click.echo("  Example: docker run -d -p 6379:6379 redis", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  ✓ Redis reachable at {redis_url}")

    if minio_url:
        minio_reachable = check_minio(minio_url)
        if not minio_reachable:
            click.echo(f"✗ Error: Cannot reach MinIO at {minio_url}", err=True)
            click.echo("  Make sure MinIO is running and accessible.", err=True)
            click.echo("  Example: docker run -d -p 27018:9000 minio/minio server /data", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  ✓ MinIO reachable at {minio_url}")

    if api_url:
        api_reachable = check_api(api_url)
        if not api_reachable:
            click.echo(f"✗ Error: Cannot reach NDIF API at {api_url}", err=True)
            click.echo("  Make sure the API service is running.", err=True)
            click.echo("  Use 'ndif start api' to start the service.", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  ✓ NDIF API reachable at {api_url}")

    if ray_address:
        ray_reachable = check_ray(ray_address)
        if not ray_reachable:
            click.echo(f"✗ Error: Cannot reach Ray at {ray_address}", err=True)
            click.echo("  Make sure the Ray service is running.", err=True)
            click.echo("  Use 'ndif start ray' to start the service.", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  ✓ Ray reachable at {ray_address}")

    if verbose:
        click.echo()
