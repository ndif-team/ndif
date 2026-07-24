"""``ndif kill`` — cancel a request by id."""

import click

from ..lib.events import kill_request


@click.command()
@click.argument("request_id")
@click.option("--redis-url", default=None, help="Redis URL (default: NDIF_REDIS_URL).")
def kill(request_id, redis_url):
    """Cancel a request by ID.

    Removes it from the queue if still waiting, or cancels it if it's already
    executing.

    \b
    Examples:
        ndif kill abc123
    """
    try:
        result = kill_request(request_id, redis_url)
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
