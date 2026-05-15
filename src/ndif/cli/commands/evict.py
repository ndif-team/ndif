"""Evict command for NDIF - evict (remove) replicas of a model deployment."""

import click

from ..lib.checks import check_prerequisites
from ..lib.evict import NDIFConnectivityError, evict as evict_lib
from ..lib.session import get_env


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('--revision', default=None, help='Model revision/branch (default: model\'s default)')
@click.option('--replica', default=None,
              help='Target a single replica by id (requires exactly one checkpoint)')
@click.option('--all', 'evict_all', is_flag=True, help='Evict every HOT deployment')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def evict(
    checkpoints: tuple,
    revision: str,
    replica: str,
    evict_all: bool,
    ray_address: str,
    broker_url: str,
):
    """Evict (remove) replicas of one or more model deployments.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
                 Optional if using --all.

    By default every HOT + WARM replica of each target model is evicted; use
    ``--replica <id>`` together with a single checkpoint to target one.

    \b
    Examples:
        ndif evict gpt2                       # all replicas of gpt2
        ndif evict gpt2 meta-llama/Llama-3.1-8b
        ndif evict gpt2 --replica abc12       # one specific replica
        ndif evict --all                      # every HOT deployment
    """
    if not evict_all and not checkpoints:
        raise click.ClickException("Must provide either CHECKPOINTS or --all")
    if evict_all and checkpoints:
        raise click.ClickException("Cannot use both CHECKPOINTS and --all flag")
    if replica is not None:
        if evict_all:
            raise click.ClickException("--replica cannot be combined with --all")
        if len(checkpoints) != 1:
            raise click.ClickException(
                "--replica targets a single replica — supply exactly one checkpoint"
            )

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    check_prerequisites(broker_url=broker_url, ray_address=ray_address)

    try:
        if evict_all:
            result = evict_lib(
                evict_all=True,
                ray_address=ray_address,
                broker_url=broker_url,
                on_message=click.echo,
            )
        else:
            result = evict_lib(
                checkpoints=[(cp, revision) for cp in checkpoints],
                replica=replica,
                ray_address=ray_address,
                broker_url=broker_url,
                on_message=click.echo,
            )

        # Summary
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
                total_gpus = sum(
                    rec["freed_gpus"] for r in evicted for rec in r["replicas"]
                )
                total_mem = sum(
                    rec["freed_memory_gbs"] for r in evicted for rec in r["replicas"]
                )
                click.echo(
                    f"✓ Successfully evicted {total_replicas} replica(s) "
                    f"across {len(evicted)} model(s)"
                )
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
