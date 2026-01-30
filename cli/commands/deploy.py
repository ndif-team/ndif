"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray
import asyncio

from ..lib.util import get_controller_actor_handle, get_model_key, notify_dispatcher
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--dedicated', is_flag=True, help='Deploy the model as dedicated - i.e. will not be evicted from hotswapping (default: False)')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
@click.option('--replicas', default=1, help='Number of replicas to deploy (default: 1)')

def deploy(checkpoint: str, revision: str, dedicated: bool, ray_address: str, broker_url: str, replicas: int):
    """Deploy a model without requiring to submit a request.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Examples:
        ndif deploy gpt2
        ndif deploy meta-llama/Llama-2-7b-hf --revision main
        ndif deploy openai-community/gpt2 --dedicated --ray-address ray://localhost:10001
        ndif deploy openai-community/gpt2 --replicas 2 --ray-address ray://localhost:10001
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

        existing_deployment = ray.get(controller.get_deployment.remote(model_key))
        if existing_deployment is not None:
            click.echo(
                f"✗ Error: {model_key} already has deployed replicas. "
                "Use `ndif scale` to add replicas."
            )
            raise click.Abort()

        click.echo(f"Deploying {model_key} with {replicas} replicas...")
        object_ref = controller._deploy.remote(model_keys=[model_key], dedicated=dedicated, replicas=replicas)
        results = ray.get(object_ref)
        result_statuses = [
            (result_key, status)
            for (result_key, status) in results["result"].items()
            if result_key[0] == model_key
        ]

        success_statuses = {
            "DEPLOYED",
            "CACHED_AND_FREE",
            "FREE",
            "CACHED_AND_FULL",
            "FULL",
        }

        successes = []
        failures = []
        for (result_key, status) in result_statuses:
            _, replica_id = result_key
            if status in success_statuses:
                successes.append((replica_id, status))
            else:
                failures.append((replica_id, status))

        total = len(result_statuses)
        if total == 0:
            click.echo("⚠ No replica results returned from controller.")
        else:
            click.echo(f"✓ Deployment result: {len(successes)}/{total} replicas succeeded.")

        if failures:
            click.echo("• Failed replicas:")
            for replica_id, status in failures:
                if status == "CANT_ACCOMMODATE":
                    click.echo(f"  - replica {replica_id}: {status}")
                else:
                    click.echo(f"  - replica {replica_id}: error during evaluation/deploy")

        if results["evictions"]:
            click.echo("• Evictions:")
            for eviction in results["evictions"]:
                click.echo(f"  - {eviction}")
                asyncio.run(notify_dispatcher(broker_url, "evict", eviction[0], replica_id=eviction[1]))

        # Notify dispatcher about deployment
        asyncio.run(notify_dispatcher(broker_url, "deploy", model_key, replicas=replicas))

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
