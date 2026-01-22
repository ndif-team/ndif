"""Stop command for NDIF services"""

import os
import signal
import subprocess
import time

import click

from .session import (
    get_current_session,
    end_session,
    get_pids_on_port,
    kill_processes_on_port,
    is_port_in_use,
)


@click.command()
@click.argument('service', type=click.Choice(
    ['api', 'ray', 'broker', 'object-store', 'all'],
    case_sensitive=False
), default='all')
@click.option('--force', is_flag=True, help='Force kill processes (SIGKILL)')
def stop(service: str, force: bool):
    """Stop NDIF services.

    SERVICE: Which service to stop (api, ray, broker, object-store, or all). Default: all

    Finds running services by checking ports and kills the associated processes.
    Uses the current session to determine which ports to check.

    Examples:
        ndif stop              # Stop all services
        ndif stop api          # Stop only API
        ndif stop ray          # Stop only Ray
        ndif stop --force      # Force kill (SIGKILL instead of SIGTERM)
    """
    session = get_current_session()

    if session is None:
        click.echo("No active session found.")
        click.echo("If services are running, you may need to stop them manually.")
        return

    click.echo(f"Session: {session.config.session_id}")
    click.echo()

    services_to_stop = []
    if service == 'all':
        # Stop in reverse order: api, ray, then dependencies
        services_to_stop = ['api', 'ray', 'broker', 'object-store']
    else:
        services_to_stop = [service]

    stopped_any = False
    sig = signal.SIGKILL if force else signal.SIGTERM

    for svc in services_to_stop:
        port = _get_service_port(session, svc)
        if port is None:
            continue

        if not is_port_in_use(port):
            if session.is_service_running(svc):
                click.echo(f"{svc}: marked as running but port {port} is not in use")
                session.mark_service_running(svc, False)
            else:
                click.echo(f"{svc}: not running")
            continue

        click.echo(f"Stopping {svc} (port {port})...")

        pids = get_pids_on_port(port)
        if not pids:
            click.echo(f"  Warning: port in use but couldn't find process")
            continue

        # Kill processes
        killed = kill_processes_on_port(port, sig)
        if killed:
            click.echo(f"  Sent signal to PIDs: {killed}")
            stopped_any = True

        # Wait for graceful shutdown
        if not force:
            time.sleep(2)

            # Check if still running
            if is_port_in_use(port):
                click.echo(f"  Still running, sending SIGKILL...")
                kill_processes_on_port(port, signal.SIGKILL)
                time.sleep(0.5)

        if not is_port_in_use(port):
            click.echo(f"  ✓ {svc} stopped")
            session.mark_service_running(svc, False)
        else:
            click.echo(f"  ✗ Failed to stop {svc}", err=True)

    # If Ray was stopped, also call `ray stop` to clean up the cluster
    if 'ray' in services_to_stop:
        click.echo("\nStopping Ray cluster...")
        try:
            result = subprocess.run(
                ['ray', 'stop'],
                capture_output=True,
                text=True,
                check=False
            )
            if result.returncode == 0:
                click.echo("✓ Ray cluster stopped")
            else:
                if "No cluster" not in result.stderr:
                    click.echo(f"Ray stop output: {result.stderr}")
        except FileNotFoundError:
            click.echo("Warning: 'ray' command not found, skipping Ray cluster cleanup")

    # End session if no services are running
    if not session.has_any_running_services():
        end_session(session)
        click.echo("\n✓ Session ended")
    elif stopped_any:
        click.echo("\n✓ Services stopped")
        remaining = session.get_running_services()
        if remaining:
            click.echo(f"Still running: {', '.join(remaining)}")
    else:
        click.echo("\nNo services were running")


def _get_service_port(session, service: str) -> int:
    """Get the port for a service from session config."""
    port_map = {
        'api': session.config.api_port,
        'ray': session.config.ray_head_port,
        'broker': session.config.broker_port,
        'object-store': session.config.object_store_port,
    }
    return port_map.get(service)
