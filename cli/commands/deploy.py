"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray


from .util import get_controller_actor_handle, get_model_key


@click.command()
@click.argument('checkpoint')
@click.option('--revision', default='main', help='Model revision/branch (default: main)')
@click.option('--dedicated', is_flag=True, help='Deploy the model as dedicated - i.e. will not be evicted from hotswapping (default: False)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
def deploy(checkpoint: str, revision: str, dedicated: bool, ray_address: str):
    """Deploy a model without requiring to submit a request.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Examples:
        ndif deploy gpt2
        ndif deploy meta-llama/Llama-2-7b-hf --revision main
        ndif deploy openai-community/gpt2 --dedicated --ray-address ray://localhost:10001
    """
    
    try:
        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")
        
        # TODO: revision bug ("main" is not always the default revision)
        model_key = get_model_key(checkpoint, revision)
        click.echo(f"Model key: {model_key}")

        # Connect to Ray
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True)

        # Get controller actor handle and deploy the model
        click.echo(f"Getting actor handle for {model_key}...")
        controller = get_controller_actor_handle()

        click.echo(f"Deploying {model_key}...")
        results = controller._deploy.remote(model_keys=[model_key], dedicated=dedicated)
        click.echo(f"Deployment results: {results}")

        click.echo("✓ Deployment successful!")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()