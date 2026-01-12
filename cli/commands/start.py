"""Start command for NDIF services"""

import os
import subprocess
import sys
from pathlib import Path

import click
from .util import get_repo_root, save_pid, clear_pid, get_pid, is_process_running, print_logo


@click.command()
@click.argument('service', type=click.Choice(['api', 'ray', 'all'], case_sensitive=False), default='all')
@click.option('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
@click.option('--port', default=8001, type=int, help='Port to bind to (API default: 8001)')
@click.option('--workers', default=1, type=int, help='Number of workers for API (default: 1)')
@click.option('--redis-url', default='redis://localhost:6379/', help='Redis URL (default: redis://localhost:6379/)')
@click.option('--minio-url', default='http://localhost:27018', help='MinIO URL (default: http://localhost:27018)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
def start(service: str, host: str, port: int, workers: int, redis_url: str,
          minio_url: str, ray_address: str):
    """Start NDIF services on bare metal (blocking, with visible logs)

    SERVICE: Which service to start (api, ray, or all). Default: all

    All services run in blocking mode with logs visible. Press Ctrl+C to stop.

    Prerequisites:
        - Redis running at localhost:6379 (or specify --redis-url)
        - MinIO running at localhost:27018 (or specify --minio-url)

    Examples:
        ndif start                              # Start all services (logs interleaved)
        ndif start api                          # Start API only
        ndif start api --port 5000              # Start API on port 5000
        ndif start ray --minio-url http://...   # Use custom MinIO URL
    """
    print_logo()
    repo_root = get_repo_root()
    processes = []

    # Start requested service(s)
    if service in ('all', 'api'):
        # Check if API is already running
        api_pid = get_pid('api')
        if api_pid and is_process_running(api_pid):
            click.echo(f"Error: API service is already running (PID: {api_pid})", err=True)
            click.echo("Run 'ndif stop' to stop all services before starting again.", err=True)
            sys.exit(1)

        proc_api = start_api(repo_root, host, port, workers, redis_url, minio_url, ray_address)
        if proc_api:
            processes.append(('api', proc_api))
            save_pid('api', proc_api.pid)

    if service in ('all', 'ray'):
        # Check if Ray is already running
        ray_pid = get_pid('ray')
        if ray_pid and is_process_running(ray_pid):
            click.echo(f"Error: Ray service is already running (PID: {ray_pid})", err=True)
            click.echo("Run 'ndif stop' to stop all services before starting again.", err=True)
            sys.exit(1)

        proc_ray = start_ray(repo_root, minio_url)
        if proc_ray:
            processes.append(('ray', proc_ray))
            save_pid('ray', proc_ray.pid)

    # Wait for all processes
    if processes:
        click.echo("\n✓ Services started. Press Ctrl+C to stop.")
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


def start_api(repo_root: Path, host: str, port: int, workers: int,
              redis_url: str, minio_url: str, ray_address: str):
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
    click.echo(f"  MinIO: {minio_url}")
    click.echo(f"  Ray: {ray_address}")
    click.echo()

    # Set up environment variables for start.sh
    env = os.environ.copy()
    env.update({
        'OBJECT_STORE_URL': minio_url,
        'BROKER_URL': redis_url,
        'WORKERS': str(workers),
        'RAY_ADDRESS': ray_address,
        'API_INTERNAL_PORT': str(port),
        'API_URL': f'http://localhost:{port}',
        'DEV_MODE': 'true',
    })

    try:
        # Start service - logs will be visible
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=api_service_dir
            # stdout and stderr go to terminal (visible logs)
        )
        return proc

    except FileNotFoundError as e:
        click.echo(f"\nError: {e}", err=True)
        click.echo("\nMake sure you're running from the NDIF repository root.", err=True)
        sys.exit(1)


def start_ray(repo_root: Path, minio_url: str):
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
    click.echo(f"  MinIO: {minio_url}")
    click.echo(f"  API: {api_url}")
    click.echo()

    # Set up environment variables for start.sh
    env = os.environ.copy()
    env.update({
        'OBJECT_STORE_URL': minio_url,
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
        # Start service - logs will be visible
        proc = subprocess.Popen(
            ['bash', str(start_script)],
            env=env,
            cwd=ray_service_dir
            # stdout and stderr go to terminal (visible logs)
        )
        return proc

    except FileNotFoundError as e:
        click.echo(f"\nError: {e}", err=True)
        click.echo("\nMake sure you're running from the NDIF repository root.", err=True)
        sys.exit(1)