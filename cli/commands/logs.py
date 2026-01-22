"""Logs command for NDIF - View service logs"""

import subprocess
import sys

import click

from ..lib.session import get_current_session, get_session_root


@click.command()
@click.argument('service', type=click.Choice(['api', 'ray', 'broker', 'object-store'], case_sensitive=False))
@click.option('-f', '--follow', is_flag=True, help='Follow log output (like tail -f)')
@click.option('-n', '--lines', type=int, default=100, help='Number of lines to show (default: 100)')
def logs(service: str, follow: bool, lines: int):
    """View logs for NDIF services.

    SERVICE: Which service logs to view (api, ray, broker, object-store)

    By default, shows the last 100 lines of the current session's logs.
    Use --follow to continuously stream new log entries.

    Examples:
        ndif logs api              # Show last 100 lines of API logs
        ndif logs broker           # Show broker (Redis) logs
        ndif logs object-store     # Show object store logs
        ndif logs ray              # Show Ray logs
        ndif logs api -n 500       # Show last 500 lines
        ndif logs api --follow     # Follow logs in real-time
    """
    session = get_current_session()

    if session is None:
        click.echo("No active session found.", err=True)
        click.echo("\nStart services with 'ndif start' first.", err=True)
        sys.exit(1)

    log_dir = session.logs_dir / service
    log_file = log_dir / "output.log"

    if not log_file.exists():
        click.echo(f"No logs found for {service} service.", err=True)
        click.echo(f"\nExpected: {log_file}", err=True)
        click.echo(f"\nMake sure the service has been started with 'ndif start {service}'", err=True)
        sys.exit(1)

    # Build tail command
    tail_cmd = ['tail']
    if follow:
        tail_cmd.append('-f')
    tail_cmd.extend(['-n', str(lines), str(log_file)])

    try:
        subprocess.run(tail_cmd)
    except KeyboardInterrupt:
        click.echo("\nStopped following logs.")
        sys.exit(0)
    except FileNotFoundError:
        click.echo("Error: 'tail' command not found.", err=True)
        sys.exit(1)
