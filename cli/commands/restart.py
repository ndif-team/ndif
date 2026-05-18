"""Restart command for NDIF - restart a model actor."""

import click
import ray

from .util import get_actor_handle, get_model_key


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default=None, help='Model revision/branch (default: auto-detect from HuggingFace)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
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
    try:
        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint} (revision: {revision or 'auto-detect'})...")
        
        model_key = get_model_key(checkpoint, revision)
        click.echo(f"Model key: {model_key}")

        # Connect to Ray
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True)

        # Get deployment actor handle and restart it
        click.echo(f"Getting actor handle for {model_key}...")
        actor = get_actor_handle(model_key)

        click.echo(f"Restarting deployment for {model_key}...")
        ray.kill(actor, no_restart=False)

        click.echo("✓ Restart successful!")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()