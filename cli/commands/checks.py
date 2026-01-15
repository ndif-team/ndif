"""Health checks and prerequisites for NDIF CLI"""

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
            click.echo(f"\u2717 Error: Cannot reach Redis at {redis_url}", err=True)
            click.echo("  Make sure Redis is running and accessible.", err=True)
            click.echo("  Example: docker run -d -p 6379:6379 redis", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  \u2713 Redis reachable at {redis_url}")

    if minio_url:
        minio_reachable = check_minio(minio_url)
        if not minio_reachable:
            click.echo(f"\u2717 Error: Cannot reach MinIO at {minio_url}", err=True)
            click.echo("  Make sure MinIO is running and accessible.", err=True)
            click.echo("  Example: docker run -d -p 27018:9000 minio/minio server /data", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  \u2713 MinIO reachable at {minio_url}")

    if api_url:
        api_reachable = check_api(api_url)
        if not api_reachable:
            click.echo(f"\u2717 Error: Cannot reach NDIF API at {api_url}", err=True)
            click.echo("  Make sure the API service is running.", err=True)
            click.echo("  Use 'ndif start api' to start the service.", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  \u2713 NDIF API reachable at {api_url}")

    if ray_address:
        ray_reachable = check_ray(ray_address)
        if not ray_reachable:
            click.echo(f"\u2717 Error: Cannot reach Ray at {ray_address}", err=True)
            click.echo("  Make sure the Ray service is running.", err=True)
            click.echo("  Use 'ndif start ray' to start the service.", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  \u2713 Ray reachable at {ray_address}")

    if verbose:
        click.echo()
