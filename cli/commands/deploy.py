"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray
import asyncio

from .util import get_controller_actor_handle, get_model_key, notify_dispatcher
from .checks import check_prerequisites
from .session import get_env


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--dedicated', is_flag=True, help='Deploy the model as dedicated - i.e. will not be evicted from hotswapping (default: False)')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def deploy(checkpoint: str, revision: str, dedicated: bool, ray_address: str, broker_url: str):
    """Deploy a model without requiring to submit a request.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Examples:
        ndif deploy gpt2
        ndif deploy meta-llama/Llama-2-7b-hf --revision main
        ndif deploy openai-community/gpt2 --dedicated --ray-address ray://localhost:10001
    """

    # Use session defaults if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        # Check prerequisites silently
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)
        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")
        
        # TODO: revision bug ("main" is not always the default revision)
        model_key = get_model_key(checkpoint, revision)
        click.echo(f"Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle and deploy the model
        click.echo(f"Getting actor handle for {model_key}...")
        controller = get_controller_actor_handle()

        click.echo(f"Deploying {model_key}...")
        object_ref = controller._deploy.remote(model_keys=[model_key], dedicated=dedicated)
        results = ray.get(object_ref)
        result_status = results["result"][model_key]

        if result_status == "CANT_ACCOMMODATE":
            click.echo(f"✗ Error: {model_key} cannot be deployed on any node. Check the ray controller logs for more details.")

        elif result_status == "DEPLOYED":
            click.echo(f"✓ {model_key} already deployed!")
        else:
            click.echo("✓ Deployment successful!")
            if results["evictions"]:
                click.echo("• Evictions:")
                for eviction in results["evictions"]:
                    click.echo(f"  - {eviction}")
                    asyncio.run(notify_dispatcher(broker_url, "evict", eviction))

            # Notify dispatcher about deployment
            asyncio.run(notify_dispatcher(broker_url, "deploy", model_key))

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()