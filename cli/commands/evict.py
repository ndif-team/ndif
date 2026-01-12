"""Evict command for NDIF - evict (remove) a model deployment."""

import click
import ray
import asyncio

from .util import get_controller_actor_handle, get_model_key, notify_dispatcher


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
@click.option('--redis-url', default='redis://localhost:6379/', help='Redis URL (default: redis://localhost:6379/)')
def evict(checkpoint: str, revision: str, ray_address: str, redis_url: str):
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
        model_key = get_model_key(checkpoint, revision)
        click.echo(f"Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle and evict the model
        click.echo(f"Getting controller handle...")
        controller = get_controller_actor_handle()

        click.echo(f"Evicting {model_key}...")
        object_ref = controller.evict.remote(model_keys=[model_key])
        results = ray.get(object_ref)

        if results[model_key]["status"] == "not_found":
            click.echo(f"✗ Error: {model_key} not found!")
        else:
            click.echo(f"✓ Eviction successful!\nGPUs freed: {results[model_key]['freed_gpus']}\nMemory freed: {round(results[model_key]['freed_memory_gbs'], 4)} GB")

            # Notify dispatcher about eviction
            asyncio.run(notify_dispatcher(redis_url, "evict", model_key))

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
