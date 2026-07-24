"""``ndif evict`` — remove replicas of one or more model deployments."""

import click

from ..lib.evict import NDIFConnectivityError, evict as evict_lib


@click.command()
@click.argument("checkpoints", nargs=-1)
@click.option("--revision", default=None, help="Model revision/branch.")
@click.option("--replica", default=None,
              help="Target a single replica by id (requires exactly one checkpoint).")
@click.option("--all", "evict_all", is_flag=True, help="Evict every HOT deployment.")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
@click.option("--redis-url", default=None, help="Redis URL for dispatcher reconcile (default: NDIF_REDIS_URL).")
def evict(checkpoints, revision, replica, evict_all, ray_address, redis_url):
    """Evict (remove) replicas of one or more model deployments.

    CHECKPOINTS: model checkpoints (optional if using --all).

    Evicts every HOT + WARM replica of each target by default; use ``--replica``
    with a single checkpoint to target one.

    \b
    Examples:
        ndif evict gpt2
        ndif evict gpt2 meta-llama/Llama-3.1-8B
        ndif evict gpt2 --replica abc12
        ndif evict --all
    """
    if not evict_all and not checkpoints:
        raise click.ClickException("Must provide either CHECKPOINTS or --all")
    if evict_all and checkpoints:
        raise click.ClickException("Cannot use both CHECKPOINTS and --all")
    if replica is not None:
        if evict_all:
            raise click.ClickException("--replica cannot be combined with --all")
        if len(checkpoints) != 1:
            raise click.ClickException(
                "--replica targets a single replica — supply exactly one checkpoint"
            )

    try:
        if evict_all:
            result = evict_lib(evict_all=True, ray_address=ray_address,
                               redis_url=redis_url, on_message=click.echo)
        else:
            result = evict_lib(
                checkpoints=[(cp, revision) for cp in checkpoints],
                replica=replica,
                ray_address=ray_address,
                redis_url=redis_url,
                on_message=click.echo,
            )

        results = result["results"]
        if not results:
            click.echo("No deployments found to evict.")
            return

        evicted = [r for r in results if r["status"] == "evicted"]
        not_found = [r for r in results if r["status"] == "not_found"]
        if len(results) > 1 or any(len(r.get("replicas", [])) > 1 for r in evicted):
            click.echo()
            if evicted:
                total_replicas = sum(len(r["replicas"]) for r in evicted)
                total_gpus = sum(rec["freed_gpus"] for r in evicted for rec in r["replicas"])
                total_mem = sum(rec["freed_memory_gbs"] for r in evicted for rec in r["replicas"])
                click.echo(f"✓ Evicted {total_replicas} replica(s) across {len(evicted)} model(s)")
                click.echo(f"  Total GPUs freed: {total_gpus}")
                click.echo(f"  Total memory freed: {round(total_mem, 4)} GB")
            if not_found:
                click.echo(f"✗ {len(not_found)} model(s) had no replicas to evict")

    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
