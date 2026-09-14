"""``ndif stop`` — tear NDIF services down."""

import shutil
import subprocess

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
        if svc.name == "ray":
            _ray_stop()
        click.echo(f"  ■ {svc.name}: stopped (was pid {pid})")


def _ray_stop() -> None:
    """Take down the Ray daemons the head left behind.

    ``ray start`` daemonises: the GCS server, raylet and autoscaler monitor
    are not children of the ``start.sh`` process group the PID file tracks,
    so killing that group stops the controller and leaves Ray itself running.
    """
    if shutil.which("ray") is None:
        return
    subprocess.run(["ray", "stop", "--force"], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, check=False)
