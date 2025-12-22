"""Evict command for NDIF - evict (remove) a model actor."""

import click
import ray

# TODO: This is a temporary workaround to get the model key. There should be a more lightweight way to do this.
from nnsight import LanguageModel

from .util import get_actor_handle


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
def evict(checkpoint: str, revision: str, ray_address: str):
    """Evict (remove) a model deployment.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    This command removes a running model deployment to free up resources.

    Examples:
        ndif evict gpt2
        ndif evict meta-llama/Llama-2-7b-hf --revision main
        ndif evict openai-community/gpt2 --ray-address ray://localhost:10001
    """
    try:
        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")

        # TODO: revision bug ("main" is not always the default revision)
        model = LanguageModel(checkpoint, revision=None, dispatch=False)
        model_key = model.to_model_key()
        click.echo(f"Model key: {model_key}")

        # Connect to Ray
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True)

        # Get deployment actor handle and delete it
        click.echo(f"Getting actor handle for {model_key}...")
        actor = get_actor_handle(model_key)

        click.echo(f"Evicting deployment for {model_key}...")
        ray.kill(actor, no_restart=True)

        click.echo("✓ Eviction successful!")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
