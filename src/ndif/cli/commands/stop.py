"""``ndif stop`` — tear NDIF services down."""

import click

from ..service import SERVICES, resolve_targets
from ..state import State
from ..util import terminate_pid


@click.command()
@click.argument("services", nargs=-1)
def stop(services):
    """Stop running services (default: all, in reverse dependency order)."""
    targets = resolve_targets(services, default=list(reversed(SERVICES)))
    state = State.from_env()
    for svc in targets:
        pid = state.running_pid(svc.name)
        if pid is None:
            click.echo(f"  ○ {svc.name}: not running")
            continue
        terminate_pid(pid)
        state.clear_pid(svc.name)
        click.echo(f"  ✗ {svc.name}: stopped (was pid {pid})")
