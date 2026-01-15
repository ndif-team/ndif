"""Stop command for NDIF services"""

import os
import signal
import subprocess
import time
from urllib.parse import urlparse

import click
from .util import get_pid, is_process_running, clear_pid, cleanup_zombie_processes, get_processes_on_port


@click.command()
@click.argument('service', type=click.Choice(['api', 'ray', 'redis', 'minio', 'all'], case_sensitive=False), default='all')
@click.option('--api-url', default='http://localhost:8001', help='API URL (used to check for zombie processes)')
def stop(service: str, api_url: str):
    """Stop NDIF services

    SERVICE: Which service to stop (api, ray, redis, minio, or all). Default: all

    Examples:
        ndif stop              # Stop all services
        ndif stop api          # Stop only API
        ndif stop ray          # Stop only Ray
        ndif stop redis        # Stop only Redis
        ndif stop minio        # Stop only MinIO
        ndif stop --api-url http://localhost:5000  # Stop with custom API URL
    """
    services_to_stop = []

    if service == 'all':
        # Stop in reverse order: api, ray, then dependencies
        services_to_stop = ['api', 'ray', 'redis', 'minio']
    else:
        services_to_stop = [service]

    stopped_any = False

    for svc in services_to_stop:
        pid = get_pid(svc)

        if pid is None:
            click.echo(f"No saved PID for {svc} service")
            continue

        if not is_process_running(pid):
            click.echo(f"{svc.upper()} service (PID {pid}) is not running")
            clear_pid(svc)
            continue

        click.echo(f"Stopping {svc.upper()} service (PID {pid})...")

        try:
            # Try graceful shutdown first - kill the entire process group
            # Using negative PID kills the process group (includes workers)
            os.killpg(os.getpgid(pid), signal.SIGTERM)

            # Wait a moment for graceful shutdown
            time.sleep(2)

            # Check if still running
            if is_process_running(pid):
                click.echo(f"  Force killing {svc.upper()} service...")
                os.killpg(os.getpgid(pid), signal.SIGKILL)

            click.echo(f"✓ {svc.upper()} service stopped")
            clear_pid(svc)
            stopped_any = True

        except (OSError, ProcessLookupError) as e:
            click.echo(f"Error stopping {svc} service: {e}", err=True)
            clear_pid(svc)

    # Failsafe: Check for zombie API processes on the port
    if 'api' in services_to_stop:
        # Parse port from API URL
        parsed = urlparse(api_url)
        api_port = parsed.port or 8001

        # Give processes a moment to fully release the port
        time.sleep(0.5)

        remaining_pids = get_processes_on_port(api_port)
        if remaining_pids:
            click.echo(f"\nWarning: Port {api_port} still in use by {len(remaining_pids)} process(es)")
            click.echo("  Checking for zombie NDIF processes...")

            if cleanup_zombie_processes(api_port, "API"):
                click.echo(f"  ✓ Cleaned up zombie processes on port {api_port}")
                # Check again
                time.sleep(0.5)
                still_remaining = get_processes_on_port(api_port)
                if still_remaining:
                    click.echo(f"  Warning: Port {api_port} still in use by non-NDIF processes", err=True)
            else:
                click.echo(f"  Warning: Port {api_port} in use by non-NDIF processes", err=True)

    # If Ray was stopped, also call `ray stop` to clean up the cluster
    if 'ray' in services_to_stop:
        click.echo("\nStopping Ray cluster...")
        try:
            result = subprocess.run(['ray', 'stop'],
                                  capture_output=True,
                                  text=True,
                                  check=False)
            if result.returncode == 0:
                click.echo("✓ Ray cluster stopped")
            else:
                # Ray stop might fail if cluster is already down, that's OK
                if "No cluster" not in result.stderr:
                    click.echo(f"Ray stop output: {result.stderr}")
        except FileNotFoundError:
            click.echo("Warning: 'ray' command not found, skipping Ray cluster cleanup")

    if stopped_any:
        click.echo("\n✓ Services stopped successfully")
    else:
        click.echo("\nNo services were running")
