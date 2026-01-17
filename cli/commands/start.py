"""Start command for NDIF services"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import click
from .util import (
    get_repo_root, save_pid, clear_pid, get_pid, is_process_running,
    print_logo, get_session_log_dir
)
from .checks import check_prerequisites, check_redis, check_minio
from .deps import start_redis as util_start_redis, start_object_store as util_start_object_store


@click.command()
@click.argument('service', type=click.Choice(['api', 'ray', 'redis', 'object-store', 'all'], case_sensitive=False), default='all')
@click.option('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
@click.option('--port', default=8001, type=int, help='Port to bind to (API default: 8001)')
@click.option('--workers', default=1, type=int, help='Number of workers for API (default: 1)')
@click.option('--redis-url', default='redis://localhost:6379/', help='Redis URL (default: redis://localhost:6379/)')
@click.option('--object-store-url', default='http://localhost:27018', help='Object store URL (default: http://localhost:27018)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
@click.option('--verbose', is_flag=True, help='Run in foreground with logs visible (blocking mode)')
def start(service: str, host: str, port: int, workers: int, redis_url: str,
          object_store_url: str, ray_address: str, verbose: bool):
    """Start NDIF services on bare metal

    SERVICE: Which service to start (api, ray, redis, object-store, or all). Default: all

    By default, services run in the background with logs redirected to /tmp/ndif/.
    Use --verbose to run in foreground with logs visible.

    When starting 'all', Redis and object store (MinIO) will be started automatically if not
    already running. They can also be started individually.

    Examples:
        ndif start                                    # Start all services in background
        ndif start api                                # Start API only
        ndif start redis                              # Start Redis only
        ndif start object-store                       # Start object store (MinIO) only
        ndif start --verbose                          # Start with logs visible
        ndif start api --port 5000                    # Start API on port 5000
        ndif start ray --object-store-url http://...  # Use custom object store URL
    """
    print_logo()
    repo_root = get_repo_root()
    processes = []

    # Parse ports from URLs for starting services
    redis_parsed = urlparse(redis_url)
    redis_port = redis_parsed.port or 6379
    object_store_parsed = urlparse(object_store_url)
    object_store_port = object_store_parsed.port or 27018

    # Start Redis if requested or if starting 'all' and Redis is not running
    if service == 'redis' or (service == 'all' and not check_redis(redis_url)):
        click.echo("Starting Redis...")
        success, pid, message = util_start_redis(port=redis_port, verbose=verbose)
        if success:
            if pid:
                save_pid('redis', pid)
                click.echo(f"  ✓ {message} (PID: {pid})")
            else:
                click.echo(f"  ✓ {message}")
        else:
            click.echo(f"  ✗ {message}", err=True)
            if service == 'redis':
                sys.exit(1)
        click.echo()

    # Start object store if requested or if starting 'all' and it's not running
    if service == 'object-store' or (service == 'all' and not check_minio(object_store_url)):
        click.echo("Starting object store (MinIO)...")
        success, pid, message = util_start_object_store(port=object_store_port, verbose=verbose)
        if success:
            if pid:
                save_pid('object-store', pid)
                click.echo(f"  ✓ {message} (PID: {pid})")
            else:
                click.echo(f"  ✓ {message}")
        else:
            click.echo(f"  ✗ {message}", err=True)
            if service == 'object-store':
                sys.exit(1)
        click.echo()

    # If only starting redis or object-store, we're done
    if service in ('redis', 'object-store'):
        click.echo("✓ Service started successfully.")
        click.echo(f"\nTo view logs: ndif logs {service}")
        click.echo("To stop: ndif stop")
        return

    # Check prerequisites for api/ray (verbose mode shows checking messages)
    check_prerequisites(redis_url=redis_url, minio_url=object_store_url, verbose=True)

    if service in ('all', 'ray'):
        # Check if Ray is already running
        ray_pid = get_pid('ray')
        if ray_pid and is_process_running(ray_pid):
            click.echo(f"Error: Ray service is already running (PID: {ray_pid})", err=True)
            click.echo("Run 'ndif stop' to stop all services before starting again.", err=True)
            sys.exit(1)

        proc_ray = start_ray(repo_root, object_store_url, verbose)
        if proc_ray:
            processes.append(('ray', proc_ray))
            save_pid('ray', proc_ray.pid)

    # Start requested service(s)
    if service in ('all', 'api'):
        # Check if API is already running
        api_pid = get_pid('api')
        if api_pid and is_process_running(api_pid):
            click.echo(f"Error: API service is already running (PID: {api_pid})", err=True)
            click.echo("Run 'ndif stop' to stop all services before starting again.", err=True)
            sys.exit(1)

        proc_api = start_api(repo_root, host, port, workers, redis_url, object_store_url, ray_address, verbose)
        if proc_api:
            processes.append(('api', proc_api))
            save_pid('api', proc_api.pid)


    # In verbose mode, wait for all processes (blocking)
    if verbose and processes:
        click.echo("\n✓ Services started in verbose mode. Press Ctrl+C to stop.")
        click.echo("=" * 60)
        try:
            for _, proc in processes:
                proc.wait()
        except KeyboardInterrupt:
            click.echo("\n\nStopping services...")
            for name, proc in processes:
                proc.terminate()
                clear_pid(name)

            # If Ray was running, call ray stop
            if any(name == 'ray' for name, _ in processes):
                click.echo("Stopping Ray cluster...")
                subprocess.run(['ray', 'stop'], check=False)

            sys.exit(0)
    elif processes:
        # Non-verbose mode - just confirm and exit
        click.echo("\n✓ Services started successfully.")
        click.echo("\nTo view logs:")
        for name, _ in processes:
            click.echo(f"  ndif logs {name}")
        click.echo("\nTo stop services:")
        click.echo("  ndif stop")


def start_api(repo_root: Path, host: str, port: int, workers: int,
              redis_url: str, object_store_url: str, ray_address: str, verbose: bool):
    """Start the API service using the existing start.sh script

    Returns:
        Process object for the running service
    """
    api_service_dir = repo_root / "src" / "services" / "api"
    start_script = api_service_dir / "start.sh"

    if not api_service_dir.exists():
        click.echo(f"Error: API service directory not found at {api_service_dir}", err=True)
        sys.exit(1)

    if not start_script.exists():
        click.echo(f"Error: start.sh script not found at {start_script}", err=True)
        sys.exit(1)

    click.echo("Starting NDIF API service...")
    click.echo(f"  Host: {host}:{port}")
    click.echo(f"  Workers: {workers}")
    click.echo(f"  Redis: {redis_url}")
    click.echo(f"  Object Store: {object_store_url}")
    click.echo(f"  Ray: {ray_address}")

    # Set up log files
    if not verbose:
        log_dir = get_session_log_dir('api')
        log_file = log_dir / "output.log"
        click.echo(f"  Logs: {log_file}")

    click.echo()

    # Set up environment variables for start.sh
    env = os.environ.copy()
    env.update({
        'OBJECT_STORE_URL': object_store_url,
        'BROKER_URL': redis_url,
        'WORKERS': str(workers),
        'RAY_ADDRESS': ray_address,
        'API_INTERNAL_PORT': str(port),
        'API_URL': f'http://localhost:{port}',
        'DEV_MODE': 'true',
    })

    try:
        if verbose:
            # Verbose mode - logs visible in terminal
            proc = subprocess.Popen(
                ['bash', str(start_script)],
                env=env,
                cwd=api_service_dir
            )
        else:
            # Non-verbose mode - redirect logs to file
            log_file_handle = open(log_file, 'w')
            proc = subprocess.Popen(
                ['bash', str(start_script)],
                env=env,
                cwd=api_service_dir,
                stdout=log_file_handle,
                stderr=subprocess.STDOUT
            )

        return proc

    except FileNotFoundError as e:
        click.echo(f"\nError: {e}", err=True)
        click.echo("\nMake sure you're running from the NDIF repository root.", err=True)
        sys.exit(1)


def start_ray(repo_root: Path, object_store_url: str, verbose: bool):
    """Start the Ray service using the existing start.sh script

    Returns:
        Process object for the running service
    """
    ray_service_dir = repo_root / "src" / "services" / "ray"
    start_script = ray_service_dir / "start.sh"

    if not ray_service_dir.exists():
        click.echo(f"Error: Ray service directory not found at {ray_service_dir}", err=True)
        sys.exit(1)

    if not start_script.exists():
        click.echo(f"Error: start.sh script not found at {start_script}", err=True)
        sys.exit(1)

    api_url = os.environ.get('API_URL', 'http://localhost:8001')
    click.echo("Starting NDIF Ray service...")
    click.echo(f"  Object Store: {object_store_url}")
    click.echo(f"  API: {api_url}")

    # Set up log files
    if not verbose:
        log_dir = get_session_log_dir('ray')
        log_file = log_dir / "output.log"
        click.echo(f"  Logs: {log_file}")

    click.echo()

    # Set up environment variables for start.sh
    env = os.environ.copy()
    env.update({
        'OBJECT_STORE_URL': object_store_url,
        'API_URL': api_url,

        # Ray ports (defaults from docker/.env)
        'RAY_HEAD_INTERNAL_PORT': os.environ.get('RAY_HEAD_INTERNAL_PORT', '6380'),
        'OBJECT_MANAGER_PORT': os.environ.get('OBJECT_MANAGER_PORT', '8076'),
        'RAY_DASHBOARD_HOST': os.environ.get('RAY_DASHBOARD_HOST', '0.0.0.0'),
        'RAY_DASHBOARD_INTERNAL_PORT': os.environ.get('RAY_DASHBOARD_INTERNAL_PORT', '8265'),
        'RAY_DASHBOARD_GRPC_PORT': os.environ.get('RAY_DASHBOARD_GRPC_PORT', '8268'),
        'RAY_SERVE_INTERNAL_PORT': os.environ.get('RAY_SERVE_INTERNAL_PORT', '8267'),

        # Controller configuration
        'NDIF_CONTROLLER_IMPORT_PATH': os.environ.get('NDIF_CONTROLLER_IMPORT_PATH',
                                                      'src.ray.deployments.controller.controller'),
        'NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS': os.environ.get('NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS', '0'),

        # Ray metrics
        'RAY_METRICS_GAUGE_EXPORT_INTERVAL_MS': os.environ.get('RAY_METRICS_GAUGE_EXPORT_INTERVAL_MS', '1000'),
        'RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S': '10',
    })

    try:
        if verbose:
            # Verbose mode - logs visible in terminal
            proc = subprocess.Popen(
                ['bash', str(start_script)],
                env=env,
                cwd=ray_service_dir
            )
        else:
            # Non-verbose mode - redirect logs to file
            log_file_handle = open(log_file, 'w')
            proc = subprocess.Popen(
                ['bash', str(start_script)],
                env=env,
                cwd=ray_service_dir,
                stdout=log_file_handle,
                stderr=subprocess.STDOUT
            )

        return proc

    except FileNotFoundError as e:
        click.echo(f"\nError: {e}", err=True)
        click.echo("\nMake sure you're running from the NDIF repository root.", err=True)
        sys.exit(1)