"""Evict command for NDIF - evict (remove) a model deployment."""

import click
import ray
import asyncio

from ..lib.util import get_controller_actor_handle, get_model_key, notify_dispatcher
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--all', 'evict_all', is_flag=True, help='Evict all HOT deployments')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def evict(checkpoints: tuple, revision: str, evict_all: bool, ray_address: str, broker_url: str):
    """Evict (remove) one or more model deployments.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
                 Optional if using --all flag

    This command removes running model deployments to free up resources.

    Examples:
        ndif evict gpt2
        ndif evict gpt2 meta-llama/Llama-3.1-8b
        ndif evict meta-llama/Llama-2-7b-hf --revision main
        ndif evict --all                               # Evict all HOT deployments
    """
    # Use session defaults if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        # Check prerequisites silently
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)

        # Validate arguments
        if not evict_all and not checkpoints:
            click.echo("✗ Error: Must provide either CHECKPOINTS or --all flag", err=True)
            raise click.Abort()

        if evict_all and checkpoints:
            click.echo("✗ Error: Cannot use both CHECKPOINTS and --all flag", err=True)
            raise click.Abort()

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle
        click.echo("Getting controller handle...")
        controller = get_controller_actor_handle()

        # Determine which model keys to evict
        if evict_all:
            # Get all deployed models from status
            click.echo("Fetching all deployments...")
            status_ref = controller.status.remote()
            status = ray.get(status_ref)

            # Extract model_keys from HOT deployments only
            deployments = status.get("deployments", {})
            model_keys = [
                deployment_info["model_key"]
                for deployment_info in deployments.values()
                if "model_key" in deployment_info and deployment_info.get("deployment_level") == "HOT"
            ]

            if not model_keys:
                click.echo("No deployments found to evict.")
                return

        else:
            # Generate model keys for all checkpoints
            model_keys = []
            for checkpoint in checkpoints:
                click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")
                # TODO: revision bug ("main" is not always the default revision)
                model_key = get_model_key(checkpoint, revision)
                model_keys.append(model_key)
                click.echo(f"  Model key: {model_key}")

        # Evict the models
        click.echo(f"Evicting {len(model_keys)} model(s)...")

        object_ref = controller.evict.remote(model_keys=model_keys)
        results = ray.get(object_ref)

        # Build model_key -> checkpoint mapping for display
        if evict_all:
            # Use repo_id from deployments dict
            key_to_name = {
                d.get("model_key"): d.get("repo_id", d.get("model_key"))
                for d in deployments.values()
            }
        else:
            # Map model_keys back to checkpoints
            key_to_name = dict(zip(model_keys, checkpoints))

        # Display results
        total_gpus_freed = 0
        total_memory_freed = 0.0
        evicted_count = 0
        not_found_count = 0

        for model_key, result in results.items():
            display_name = key_to_name.get(model_key, model_key)

            if result["status"] == "not_found":
                if len(model_keys) == 1:
                    click.echo(f"✗ {display_name} not found")
                else:
                    click.echo(f"  ✗ {display_name}: not found")
                not_found_count += 1
            else:
                if len(model_keys) == 1:
                    click.echo(f"✓ Evicted {display_name}")
                    click.echo(f"  GPUs freed: {result['freed_gpus']}")
                    click.echo(f"  Memory freed: {round(result['freed_memory_gbs'], 4)} GB")
                else:
                    click.echo(f"  ✓ {display_name}: evicted")

                total_gpus_freed += result['freed_gpus']
                total_memory_freed += result['freed_memory_gbs']
                evicted_count += 1

                # Notify dispatcher about eviction
                asyncio.run(notify_dispatcher(broker_url, "evict", model_key))

        # Summary (only for multiple models)
        if len(model_keys) > 1:
            click.echo()
            if evicted_count > 0:
                click.echo(f"✓ Successfully evicted {evicted_count} model(s)")
                click.echo(f"  Total GPUs freed: {total_gpus_freed}")
                click.echo(f"  Total memory freed: {round(total_memory_freed, 4)} GB")

            if not_found_count > 0:
                click.echo(f"✗ {not_found_count} model(s) not found")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
