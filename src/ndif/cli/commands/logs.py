"""``ndif logs`` — view a service's captured output."""

import subprocess

import click

from ..service import SERVICE_MAP
from ..state import State


@click.command()
@click.argument("service", type=click.Choice(list(SERVICE_MAP)))
@click.option("-f", "--follow", is_flag=True, help="Follow log output (like tail -f).")
@click.option("-n", "--lines", type=int, default=100, help="Lines to show (default: 100).")
def logs(service, follow, lines):
    """Show a service's log at $NDIF_HOME/logs/<service>.log.

    Only detached services (the default `ndif start`) capture a log file; a
    foreground service writes straight to the terminal.
    """
    log_file = State.from_env().log_file(service)
    if not log_file.exists():
        raise click.ClickException(
            f"No logs for {service} at {log_file}. Start it with `ndif start {service}`."
        )

    cmd = ["tail"]
    if follow:
        cmd.append("-f")
    cmd += ["-n", str(lines), str(log_file)]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        click.echo("\nStopped following logs.")
    except FileNotFoundError:
        raise click.ClickException("`tail` not found.")
