"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray
import asyncio

from ..lib.util import get_controller_actor_handle, get_model_key, notify_dispatcher
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


@click.command()
@click.argument('checkpoints', nargs=-1, required=True)
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--dedicated', is_flag=True, help='Deploy the model as dedicated - i.e. will not be evicted from hotswapping (default: False)')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def deploy(checkpoints: tuple, revision: str, dedicated: bool, ray_address: str, broker_url: str):
    """Deploy one or more models without requiring to submit a request.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 meta-llama/Llama-3.1-8b
        ndif deploy gpt2 meta-llama/Llama-3.1-8b --dedicated
        ndif deploy meta-llama/Llama-2-7b-hf --revision main
    """

    # Use session defaults if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        # Check prerequisites silently
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)

        # Generate model keys for all checkpoints
        model_keys = []
        for checkpoint in checkpoints:
            click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")
            # TODO: revision bug ("main" is not always the default revision)
            model_key = get_model_key(checkpoint, revision)
            model_keys.append(model_key)
            click.echo(f"  Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle
        controller = get_controller_actor_handle()

        # Deploy all models
        click.echo(f"Deploying {len(model_keys)} model(s)...")
        object_ref = controller._deploy.remote(model_keys=model_keys, dedicated=dedicated)
        results = ray.get(object_ref)

        # Report results for each model
        all_evictions = results.get("evictions", [])
        for model_key in model_keys:
            result_status = results["result"].get(model_key, "UNKNOWN")

            if result_status == "CANT_ACCOMMODATE":
                click.echo(f"✗ {model_key}: cannot be deployed on any node")
            elif result_status == "DEPLOYED":
                click.echo(f"✓ {model_key}: already deployed")
            else:
                click.echo(f"✓ {model_key}: deployed successfully")
                # Notify dispatcher about deployment
                asyncio.run(notify_dispatcher(broker_url, "deploy", model_key))

        # Report evictions once at the end
        if all_evictions:
            click.echo("\nEvictions:")
            for eviction in all_evictions:
                click.echo(f"  - {eviction}")
                asyncio.run(notify_dispatcher(broker_url, "evict", eviction))

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()