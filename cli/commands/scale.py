"""Scale command for NDIF - Scale a deployed model to an exact replica count."""

import click
import ray

from ..lib.util import get_controller_actor_handle, get_model_key
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


@click.command()
@click.argument("checkpoint")
@click.option("--revision", default="main", help="Model revision/branch (default: main)")
@click.option(
    "--ray-address", default=None, help="Ray address (default: from NDIF_RAY_ADDRESS)"
)
@click.option(
    "--replicas", required=True, type=int, help="Target replica count (required)"
)
@click.option(
    "--dedicated",
    is_flag=True,
    help="Treat deployment as dedicated when scaling (default: False)",
)
@click.option(
    "--scale-up",
    is_flag=True,
    help="Scale up the model by the number of replicas specified (default: False)",
)

def scale(
    checkpoint: str,
    revision: str,
    ray_address: str,
    replicas: int,
    dedicated: bool,
    scale_up: bool,
):
    """Scale a model to an exact replica count.

    CHECKPOINT: Model checkpoint (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")
    """

    # Use session defaults if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")

    try:
        # Check prerequisites silently
        check_prerequisites(ray_address=ray_address)
        # Generate model_key using nnsight (loads to meta device, no actual model loading)
        click.echo(f"Generating model key for {checkpoint} (revision: {revision})...")

        # TODO: revision bug ("main" is not always the default revision)
        model_key = get_model_key(checkpoint, revision)
        click.echo(f"Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle and scale the model
        click.echo(f"Getting actor handle for {model_key}...")
        controller = get_controller_actor_handle()

        if scale_up:
            click.echo(f"Scaling up {model_key} by {replicas} replicas...")
            object_ref = controller.scale_up.remote(
                model_key=model_key, replicas=replicas, dedicated=dedicated
            )
        else:
            click.echo(f"Scaling {model_key} to {replicas} replicas...")
            object_ref = controller.scale.remote(
                model_key=model_key, replicas=replicas, dedicated=dedicated
            )
        results = ray.get(object_ref)

        deploy_results = results.get("deploy", {})
        current_replicas = results.get("current_replicas")
        target_replicas = results.get("target_replicas")
        changed = results.get("changed", False)

        result_statuses = [
            (result_key, status)
            for (result_key, status) in deploy_results.get("result", {}).items()
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
        if not changed:
            if current_replicas is not None and target_replicas is not None:
                click.echo(
                    f"✓ No changes: current replicas ({current_replicas}) >= target ({target_replicas})."
                )
            else:
                click.echo("✓ No changes: current replicas already meet or exceed target.")
        elif total == 0:
            click.echo("⚠ No replica results returned from controller.")
        else:
            click.echo(f"✓ Scale result: {len(successes)}/{total} replicas ready.")

        if failures:
            click.echo("• Failed replicas:")
            for replica_id, status in failures:
                if status == "CANT_ACCOMMODATE":
                    click.echo(f"  - replica {replica_id}: {status}")
                else:
                    click.echo(f"  - replica {replica_id}: error during evaluation/deploy")

        if deploy_results.get("evictions"):
            click.echo("• Evictions:")
            for eviction in deploy_results["evictions"]:
                click.echo(f"  - {eviction}")

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
