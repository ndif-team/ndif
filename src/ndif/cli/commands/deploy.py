"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

from pathlib import Path
from typing import Optional

import click

from ..lib.checks import check_prerequisites
from ..lib.deploy import NDIFConnectivityError, deploy as deploy_lib
from ..lib.model_config import load_model_config
from ..lib.session import get_env


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('-f', '--file', 'config_file', type=click.Path(), help='YAML config file with model specs')
@click.option('--sync', is_flag=True, help='Sync mode: evict models not in config file (requires -f)')
@click.option('--revision', default=None, help='Model revision/branch (default: unset/None)')
@click.option('--pinned', is_flag=True, help='Deploy as pinned - will not be evicted (default: False)')
@click.option('--actor-class', 'actor_class', default=None,
              help='Dotted import path of the Ray actor class to use (default: the '
                   "controller's configured default, typically ModelActor). "
                   'With -f, acts as the default for entries that do not set actor_class themselves.')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def deploy(checkpoints: tuple, config_file: str, sync: bool, revision: Optional[str], pinned: bool, actor_class: str, ray_address: str, broker_url: str):
    """Deploy one or more models without requiring to submit a request.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Models can be specified as arguments or via a config file (-f).
    When using -f, per-model revision/pinned settings in the file take precedence.

    Use --sync with -f to make the cluster state match the config file exactly,
    evicting any models not specified in the file.

    \b
    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 meta-llama/Llama-3.1-8b
        ndif deploy gpt2 --pinned
        ndif deploy -f models.yaml
        ndif deploy -f models.yaml --sync    # Evict models not in file
    """
    if not checkpoints and not config_file:
        raise click.ClickException("Must specify either CHECKPOINTS or --file/-f")
    if sync and not config_file:
        raise click.ClickException("--sync requires --file/-f")
    if sync and checkpoints:
        raise click.ClickException("--sync cannot be used with checkpoint arguments")

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    # check_prerequisites prints helpful diagnostics + exits on failure;
    # the lib's own check is silent and used when called from non-CLI callers.
    check_prerequisites(broker_url=broker_url, ray_address=ray_address)

    if config_file:
        try:
            specs = load_model_config(
                Path(config_file),
                default_revision=revision,
                default_pinned=pinned,
                default_model_actor_class=actor_class,
            )
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))
        click.echo(f"Loaded {len(specs)} model(s) from {config_file}")
    else:
        specs = [
            {"checkpoint": cp, "revision": revision,
             "pinned": pinned, "actor_class": actor_class}
            for cp in checkpoints
        ]

    try:
        deploy_lib(
            specs,
            sync=sync,
            ray_address=ray_address,
            broker_url=broker_url,
            on_message=click.echo,
        )
    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
