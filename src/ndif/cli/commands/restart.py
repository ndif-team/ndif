"""Restart command for NDIF - restart a model actor."""

import click
from typing import Optional

from ..lib.checks import check_prerequisites
from ..lib.restart import NDIFConnectivityError, restart as restart_lib
from ..lib.session import get_env


@click.command()
@click.argument("checkpoint")
@click.option(
    "--revision", default=None, help="Model revision/branch (default: unset/None)"
)
@click.option(
    "--replica",
    default=None,
    help="Target a single replica by id (default: restart all replicas)",
)
@click.option(
    "--ray-address", default=None, help="Ray address (default: from NDIF_RAY_ADDRESS)"
)
def restart(checkpoint: str, revision: str, replica: str, ray_address: str):
    """Restart replicas of a model deployment.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Restarts every HOT replica of the model by default; use ``--replica``
    to target a single one. Useful for clearing cached state, reloading
    model weights, or recovering from errors.

    \b
    Examples:
        ndif restart gpt2
        ndif restart gpt2 --replica abc12
        ndif restart meta-llama/Llama-2-7b-hf --revision main
    """
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    check_prerequisites(ray_address=ray_address)

    try:
        restart_lib(
            checkpoint,
            revision=revision,
            replica=replica,
            ray_address=ray_address,
            on_message=click.echo,
        )
    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
