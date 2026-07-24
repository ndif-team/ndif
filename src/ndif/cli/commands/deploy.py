"""``ndif deploy`` — deploy one or more models on the cluster."""

from pathlib import Path

import click

from ..lib.deploy import NDIFConnectivityError, deploy as deploy_lib
from ..lib.model_config import load_model_config


@click.command()
@click.argument("checkpoints", nargs=-1)
@click.option("-f", "--file", "config_file", type=click.Path(),
              help="YAML config file with model specs.")
@click.option("--sync", is_flag=True,
              help="Match the cluster to the config file exactly (requires -f).")
@click.option("--revision", default=None, help="Model revision/branch (default: unset).")
@click.option("--pinned", is_flag=True, help="Deploy as pinned (won't be evicted).")
@click.option("--replicas", type=int, default=1, show_default=True,
              help="New replicas to add per model (deploy is always additive).")
@click.option("--actor-class", "actor_class", default=None,
              help="Dotted import path of the Ray actor class (default: controller's).")
@click.option("--trusted", is_flag=True,
              help="Run the model's own repo code (HF trust_remote_code) and skip the sandbox.")
@click.option("--dtype", default=None,
              help="Torch dtype to load and size the model in (default: controller's).")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
@click.option("--redis-url", default=None, help="Redis URL for dispatcher reconcile (default: NDIF_REDIS_URL).")
def deploy(checkpoints, config_file, sync, revision, pinned, replicas, actor_class,
           trusted, dtype, ray_address, redis_url):
    """Deploy one or more models.

    CHECKPOINTS: model checkpoints (e.g. "gpt2", "meta-llama/Llama-3.1-8B").

    Additive by default — each call adds --replicas new replicas per model. Use
    ``-f config.yaml --sync`` to reconcile the cluster to an exact desired state.

    \b
    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 --replicas 3
        ndif deploy gpt2 meta-llama/Llama-3.1-8B
        ndif deploy gpt2 --pinned
        ndif deploy -f models.yaml
        ndif deploy -f models.yaml --sync
    """
    if not checkpoints and not config_file:
        raise click.ClickException("Must specify either CHECKPOINTS or --file/-f")
    if sync and not config_file:
        raise click.ClickException("--sync requires --file/-f")
    if sync and checkpoints:
        raise click.ClickException("--sync cannot be used with checkpoint arguments")
    if replicas < 1:
        raise click.ClickException("--replicas must be >= 1")

    if config_file:
        try:
            specs = load_model_config(
                Path(config_file),
                default_revision=revision,
                default_pinned=pinned,
                default_replicas=replicas,
                default_model_actor_class=actor_class,
                default_trusted=trusted,
                default_dtype=dtype,
            )
        except (FileNotFoundError, ValueError) as e:
            raise click.ClickException(str(e))
        click.echo(f"Loaded {len(specs)} model(s) from {config_file}")
    else:
        specs = [
            {"checkpoint": cp, "revision": revision, "pinned": pinned,
             "replicas": replicas, "actor_class": actor_class,
             "trusted": trusted, "dtype": dtype}
            for cp in checkpoints
        ]

    try:
        deploy_lib(specs, sync=sync, ray_address=ray_address, redis_url=redis_url,
                   on_message=click.echo)
    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
