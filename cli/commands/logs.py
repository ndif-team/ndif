"""Logs command for NDIF - View service logs"""

import subprocess
import sys
from pathlib import Path

import click
from .util import get_log_dir


@click.command()
@click.argument('service', type=click.Choice(['api'], case_sensitive=False))
@click.option('-f', '--follow', is_flag=True, help='Follow log output (like tail -f)')
@click.option('-n', '--lines', type=int, default=100, help='Number of lines to show (default: 100)')
def logs(service: str, follow: bool, lines: int):
    """View logs for NDIF services

    SERVICE: Which service logs to view (currently only 'api' supported)

    By default, shows the last 100 lines of the most recent log session.
    Use --follow to continuously stream new log entries.

    Examples:
        ndif logs api              # Show last 100 lines
        ndif logs api -n 500       # Show last 500 lines
        ndif logs api --follow     # Follow logs in real-time
        ndif logs api -f -n 50     # Follow, starting from last 50 lines
    """
    log_dir = get_log_dir() / service
    latest_link = log_dir / "latest"

    # Check if logs exist
    if not latest_link.exists():
        click.echo(f"No logs found for {service} service.", err=True)
        click.echo(f"\nExpected log directory: {log_dir}", err=True)
        click.echo(f"\nMake sure the service has been started at least once with 'ndif start {service}'", err=True)
        sys.exit(1)

    log_file = latest_link / "output.log"

    if not log_file.exists():
        click.echo(f"Log file not found: {log_file}", err=True)
        sys.exit(1)

    # Build tail command
    tail_cmd = ['tail']
    if follow:
        tail_cmd.append('-f')
    tail_cmd.extend(['-n', str(lines), str(log_file)])

    try:
        # Run tail command - output goes directly to terminal
        subprocess.run(tail_cmd)
    except KeyboardInterrupt:
        # Clean exit on Ctrl+C when following logs
        click.echo("\nStopped following logs.")
        sys.exit(0)
    except FileNotFoundError:
        click.echo("Error: 'tail' command not found.", err=True)
        sys.exit(1)
