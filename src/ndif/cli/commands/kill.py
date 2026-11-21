"""``ndif kill`` — cancel a request by id."""

import click

from ..lib.dispatcher import kill as kill_lib


@click.command()
@click.argument("request_id")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
def kill(request_id, ray_address):
    """Cancel a request by ID.

    Removes it from the queue if still waiting, or cancels it if it's already
    executing.

    \b
    Examples:
        ndif kill abc123
    """
    try:
        result = kill_lib(request_id, ray_address=ray_address)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()

    status = result.get("status")
    message = result.get("message", "")

    if status in ("removed_from_queue", "cancelled_execution"):
        click.echo(f"✓ {message}")
    elif status == "not_found":
        click.echo(f"✗ {message}", err=True)
        click.echo("   Hint: use 'ndif queue' to see active requests", err=True)
        raise click.Abort()
    else:
        click.echo(f"✗ {message or status}", err=True)
        raise click.Abort()
