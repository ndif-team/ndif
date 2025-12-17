"""Stop command for NDIF services"""

import os
import signal
import subprocess

import click
from .util import get_pid, is_process_running, clear_pid


@click.command()
@click.argument('service', type=click.Choice(['api', 'ray', 'all'], case_sensitive=False), default='all')
def stop(service: str):
    """Stop NDIF services

    SERVICE: Which service to stop (api, ray, or all). Default: all

    Examples:
        ndif stop              # Stop all services
        ndif stop api          # Stop only API
        ndif stop ray          # Stop only Ray
    """
    services_to_stop = []

    if service == 'all':
        services_to_stop = ['api', 'ray']
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
            import time
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
