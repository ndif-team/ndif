"""Evict command for NDIF - evict (remove) a model deployment."""

import click

from ..lib.checks import check_prerequisites
from ..lib.evict import NDIFConnectivityError, evict as evict_lib
from ..lib.session import get_env


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('--revision', default=None, help='Model revision/branch (default: model\'s default)')
@click.option('--all', 'evict_all', is_flag=True, help='Evict all HOT deployments')
@click.option('--flush-cache', 'flush_cache', is_flag=True, help='Flush all WARM models from CPU cache')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def evict(checkpoints: tuple, revision: str, evict_all: bool, flush_cache: bool, ray_address: str, broker_url: str):
    """Evict (remove) one or more model deployments.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
                 Optional if using --all or --flush-cache flags

    This command removes running model deployments to free up resources.
    Use --flush-cache to clear all WARM (CPU-cached) models.

    \b
    Examples:
        ndif evict gpt2
        ndif evict gpt2 meta-llama/Llama-3.1-8b
        ndif evict --all          # Evict all HOT deployments
        ndif evict --flush-cache  # Flush all WARM cache
    """
    if flush_cache and (checkpoints or evict_all):
        raise click.ClickException("--flush-cache cannot be combined with checkpoints or --all")
    if not flush_cache and not evict_all and not checkpoints:
        raise click.ClickException("Must provide either CHECKPOINTS, --all, or --flush-cache")
    if evict_all and checkpoints:
        raise click.ClickException("Cannot use both CHECKPOINTS and --all flag")

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    check_prerequisites(broker_url=broker_url, ray_address=ray_address)

    try:
        if flush_cache:
            result = evict_lib(
                flush_cache=True,
                ray_address=ray_address,
                broker_url=broker_url,
                on_message=click.echo,
            )
            if not result["flushed"]:
                click.echo("No WARM models to flush.")
            else:
                gb = result["memory_freed_bytes"] / (1024 ** 3)
                click.echo(f"\n✓ Flushed {len(result['flushed'])} WARM model(s), freed {gb:.2f} GB")
            return

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
        if len(results) > 1:
            click.echo()
            if evicted:
                total_gpus = sum(r["freed_gpus"] for r in evicted)
                total_mem = sum(r["freed_memory_gbs"] for r in evicted)
                click.echo(f"✓ Successfully evicted {len(evicted)} model(s)")
                click.echo(f"  Total GPUs freed: {total_gpus}")
                click.echo(f"  Total memory freed: {round(total_mem, 4)} GB")
            if not_found:
                click.echo(f"✗ {len(not_found)} model(s) not found")

    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
