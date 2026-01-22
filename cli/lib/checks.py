"""Health checks and prerequisites for NDIF CLI.

This module provides:
1. Service connectivity checks (Redis, MinIO, API, Ray)
2. Pre-flight validation for starting services
3. Detailed error diagnostics with actionable suggestions
"""

import os
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


@dataclass
class CheckResult:
    """Result of a health check."""
    success: bool
    message: str
    details: Optional[str] = None
    suggestion: Optional[str] = None

    def __bool__(self):
        return self.success


# =============================================================================
# Service Connectivity Checks
# =============================================================================


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


def check_prerequisites(broker_url: str = None, minio_url: str = None,
                       api_url: str = None, ray_address: str = None,
                       verbose: bool = False):
    """Check prerequisites and exit if any are unreachable.

    Args:
        broker_url: Broker (Redis) URL to check (optional)
        minio_url: MinIO URL to check (optional)
        api_url: API URL to check (optional)
        ray_address: Ray address to check (optional)
        verbose: If True, show checking messages and success. If False, only show errors.
    """
    import click
    import sys

    if verbose:
        click.echo("Checking prerequisites...")

    if broker_url:
        broker_reachable = check_redis(broker_url)
        if not broker_reachable:
            click.echo(f"\u2717 Error: Cannot reach broker at {broker_url}", err=True)
            click.echo("  Make sure the broker (Redis) is running and accessible.", err=True)
            click.echo("  Use 'ndif start broker' to start it.", err=True)
            sys.exit(1)
        if verbose:
            click.echo(f"  \u2713 Broker reachable at {broker_url}")

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


# =============================================================================
# Pre-flight Checks (Detailed Diagnostics)
# =============================================================================


def check_directory_writable(path: str | Path) -> CheckResult:
    """Check if a directory exists and is writable.

    Args:
        path: Directory path to check

    Returns:
        CheckResult with detailed diagnostics
    """
    path = Path(path)

    # Check if parent exists
    if not path.parent.exists():
        return CheckResult(
            success=False,
            message=f"Parent directory does not exist: {path.parent}",
            suggestion=f"Create the parent directory: mkdir -p {path.parent}"
        )

    # Try to create the directory if it doesn't exist
    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return CheckResult(
                success=False,
                message=f"Cannot create directory: {path}",
                details=f"Permission denied. Directory is owned by another user.",
                suggestion=f"Use a different path or fix permissions: sudo chown $USER {path.parent}"
            )
        except OSError as e:
            return CheckResult(
                success=False,
                message=f"Cannot create directory: {path}",
                details=str(e)
            )

    # Check if we can write to it
    try:
        test_file = path / ".ndif_write_test"
        test_file.touch()
        test_file.unlink()
        return CheckResult(success=True, message=f"Directory writable: {path}")
    except PermissionError:
        # Get owner info
        try:
            import pwd
            stat_info = path.stat()
            owner = pwd.getpwuid(stat_info.st_uid).pw_name
            details = f"Directory owned by '{owner}', current user is '{os.environ.get('USER', 'unknown')}'"
        except (ImportError, KeyError):
            details = "Permission denied"

        return CheckResult(
            success=False,
            message=f"Cannot write to directory: {path}",
            details=details,
            suggestion=f"Use a different path by setting NDIF_SESSION_ROOT or NDIF_RAY_TEMP_DIR"
        )


def check_port_available(port: int, service_name: str = "service") -> CheckResult:
    """Check if a port is available for binding.

    Args:
        port: Port number to check
        service_name: Name of service (for error messages)

    Returns:
        CheckResult with detailed diagnostics
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            result = s.connect_ex(("localhost", port))

            if result == 0:
                # Port is in use, try to find what's using it
                details = _get_port_user(port)
                return CheckResult(
                    success=False,
                    message=f"Port {port} is already in use",
                    details=details,
                    suggestion=f"Stop the process using port {port}, or use a different port for {service_name}"
                )

            return CheckResult(success=True, message=f"Port {port} is available")

    except socket.error as e:
        return CheckResult(
            success=False,
            message=f"Error checking port {port}",
            details=str(e)
        )


def _get_port_user(port: int) -> str:
    """Get information about what process is using a port."""
    import subprocess

    try:
        # Try lsof first
        result = subprocess.run(
            ["lsof", "-i", f":{port}", "-P", "-n"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = result.stdout.strip().split("\n")
            if len(lines) > 1:
                # Parse the second line (first is header)
                parts = lines[1].split()
                if len(parts) >= 2:
                    return f"Process: {parts[0]} (PID: {parts[1]})"

        # Try ss as fallback
        result = subprocess.run(
            ["ss", "-tlnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    except FileNotFoundError:
        pass

    return "Unable to determine which process is using the port"


def check_command_available(command: str) -> CheckResult:
    """Check if a command is available in PATH.

    Args:
        command: Command name to check

    Returns:
        CheckResult with detailed diagnostics
    """
    path = shutil.which(command)
    if path:
        return CheckResult(
            success=True,
            message=f"Command '{command}' found at {path}"
        )

    return CheckResult(
        success=False,
        message=f"Command '{command}' not found in PATH",
        suggestion=f"Install {command} or ensure it's in your PATH"
    )


def check_ray_temp_dir(temp_dir: str | Path) -> CheckResult:
    """Check Ray temp directory specifically.

    This is a common failure point - Ray needs a writable temp directory.

    Args:
        temp_dir: Ray temp directory path

    Returns:
        CheckResult with detailed diagnostics
    """
    temp_dir = Path(temp_dir)

    # First check basic writability
    result = check_directory_writable(temp_dir)
    if not result.success:
        result.suggestion = (
            f"Set NDIF_RAY_TEMP_DIR to a writable location.\n"
            f"  Example: export NDIF_RAY_TEMP_DIR=/tmp/$USER/ray"
        )
        return result

    # Check for common Ray temp dir issues
    # Ray creates subdirectories, so we need enough space
    try:
        import shutil
        total, used, free = shutil.disk_usage(temp_dir)
        free_gb = free / (1024**3)
        if free_gb < 1:
            return CheckResult(
                success=False,
                message=f"Insufficient disk space in {temp_dir}",
                details=f"Only {free_gb:.1f} GB free, Ray needs at least 1 GB",
                suggestion="Free up disk space or use a different temp directory"
            )
    except OSError:
        pass

    return CheckResult(success=True, message=f"Ray temp directory OK: {temp_dir}")


def preflight_check_api(
    host: str,
    port: int,
    broker_url: str,
    object_store_url: str,
    skip_broker_check: bool = False,
    skip_object_store_check: bool = False,
) -> list[CheckResult]:
    """Run pre-flight checks before starting the API service.

    Args:
        host: Host to bind to
        port: Port to bind to
        broker_url: Redis/broker URL
        object_store_url: Object store URL
        skip_broker_check: Skip broker connectivity check (if starting broker in same command)
        skip_object_store_check: Skip object store check (if starting it in same command)

    Returns:
        List of CheckResults (all must pass)
    """
    results = []

    # Check port availability
    results.append(check_port_available(port, "API"))

    # Check broker is reachable (unless we're starting it)
    if not skip_broker_check:
        if check_redis(broker_url):
            results.append(CheckResult(success=True, message=f"Broker reachable at {broker_url}"))
        else:
            results.append(CheckResult(
                success=False,
                message=f"Cannot reach broker at {broker_url}",
                suggestion="Start the broker first: ndif start broker"
            ))

    # Check object store is reachable (unless we're starting it)
    if not skip_object_store_check:
        if check_minio(object_store_url):
            results.append(CheckResult(success=True, message=f"Object store reachable at {object_store_url}"))
        else:
            results.append(CheckResult(
                success=False,
                message=f"Cannot reach object store at {object_store_url}",
                suggestion="Start the object store first: ndif start object-store"
            ))

    return results


def preflight_check_ray(
    temp_dir: str,
    object_store_url: str,
    head_port: int,
    dashboard_port: int,
    object_manager_port: int,
    grpc_port: int,
    serve_port: int,
    skip_object_store_check: bool = False,
) -> list[CheckResult]:
    """Run pre-flight checks before starting the Ray service.

    Args:
        temp_dir: Ray temp directory
        object_store_url: Object store URL
        head_port: Ray head node port
        dashboard_port: Ray dashboard port
        object_manager_port: Ray object manager port
        grpc_port: Ray dashboard gRPC port
        serve_port: Ray serve/metrics port
        skip_object_store_check: Skip object store check (if starting it in same command)

    Returns:
        List of CheckResults (all must pass)
    """
    results = []

    # Check temp directory
    results.append(check_ray_temp_dir(temp_dir))

    # Check ray command is available
    results.append(check_command_available("ray"))

    # Check all Ray ports are available
    ray_ports = [
        (head_port, "Ray head"),
        (dashboard_port, "Ray dashboard"),
        (object_manager_port, "Ray object manager"),
        (grpc_port, "Ray dashboard gRPC"),
        (serve_port, "Ray serve/metrics"),
    ]
    for port, name in ray_ports:
        results.append(check_port_available(port, name))

    # Check object store is reachable (unless we're starting it)
    if not skip_object_store_check:
        if check_minio(object_store_url):
            results.append(CheckResult(success=True, message=f"Object store reachable at {object_store_url}"))
        else:
            results.append(CheckResult(
                success=False,
                message=f"Cannot reach object store at {object_store_url}",
                suggestion="Start the object store first: ndif start object-store"
            ))

    return results


def preflight_check_broker(port: int) -> list[CheckResult]:
    """Run pre-flight checks before starting the broker service.

    Args:
        port: Broker port

    Returns:
        List of CheckResults (all must pass)
    """
    results = []
    results.append(check_port_available(port, "broker"))
    return results


def preflight_check_object_store(port: int) -> list[CheckResult]:
    """Run pre-flight checks before starting the object store service.

    Args:
        port: Object store port

    Returns:
        List of CheckResults (all must pass)
    """
    results = []
    results.append(check_port_available(port, "object-store"))
    return results


def preflight_check_worker(temp_dir: str, ray_address: str) -> list[CheckResult]:
    """Run pre-flight checks before starting a Ray worker node.

    Args:
        temp_dir: Ray temp directory
        ray_address: Ray head address to connect to

    Returns:
        List of CheckResults (all must pass)
    """
    results = []

    # Check temp directory
    results.append(check_ray_temp_dir(temp_dir))

    # Check ray command is available
    results.append(check_command_available("ray"))

    # Check Ray head is reachable
    if check_ray(ray_address):
        results.append(CheckResult(success=True, message=f"Ray head reachable at {ray_address}"))
    else:
        results.append(CheckResult(
            success=False,
            message=f"Cannot reach Ray head at {ray_address}",
            suggestion="Ensure the head node is running and NDIF_RAY_ADDRESS is correct"
        ))

    return results


def run_preflight_checks(checks: list[CheckResult], verbose: bool = True) -> bool:
    """Run a list of pre-flight checks and report results.

    Args:
        checks: List of CheckResults to evaluate
        verbose: If True, show all results. If False, only show failures.

    Returns:
        True if all checks passed, False otherwise
    """
    import click

    all_passed = True

    for check in checks:
        if check.success:
            if verbose:
                click.echo(f"  ✓ {check.message}")
        else:
            all_passed = False
            click.echo(f"  ✗ {check.message}", err=True)
            if check.details:
                click.echo(f"    Details: {check.details}", err=True)
            if check.suggestion:
                click.echo(f"    Suggestion: {check.suggestion}", err=True)

    return all_passed
