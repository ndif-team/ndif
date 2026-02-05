"""Restart command for NDIF - restart a model actor."""

import click
import ray

# TODO: This is a temporary workaround to get the model key. There should be a more lightweight way to do this.
from nnsight import LanguageModel

from ..lib.util import get_actor_handle
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default=None, help='Model revision/branch (default: model\'s default)')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
def restart(checkpoint: str, revision: str, ray_address: str):
    """Restart a model deployment.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    This command restarts a running model deployment, useful for:
    - Clearing cached state
    - Reloading model weights
    - Recovering from errors

    Examples:
        ndif restart gpt2
        ndif restart meta-llama/Llama-2-7b-hf --revision main
        ndif restart openai-community/gpt2 --ray-address ray://localhost:10001
    """
    # Use session default if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    try:
        # Check prerequisites silently
        check_prerequisites(ray_address=ray_address)

        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint}{f' (revision: {revision})' if revision else ''}...")

        model = LanguageModel(checkpoint, revision=revision, dispatch=False)
        model_key = model.to_model_key()
        click.echo(f"Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get deployment actor handle and restart it
        click.echo(f"Getting actor handle for {model_key}...")
        actor = get_actor_handle(model_key)

        click.echo(f"Restarting deployment for {model_key}...")
        ray.kill(actor, no_restart=False)

        click.echo("✓ Restart successful!")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()