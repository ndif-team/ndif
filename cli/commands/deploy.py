"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray
import asyncio
from pathlib import Path

import yaml

from ..lib.util import get_controller_actor_handle, get_model_key, notify_dispatcher
from ..lib.checks import check_prerequisites
from ..lib.session import get_env


def _parse_model_specs(file_path: str, cli_revision: str, cli_dedicated: bool) -> list[dict]:
    """Parse model specifications from a YAML config file.

    Supports two formats:
      - Simple: "gpt2" (string, uses defaults)
      - Full: {checkpoint: "gpt2", revision: "main", dedicated: true}

    Args:
        file_path: Path to the YAML config file
        cli_revision: Default revision from CLI (used if not specified in file)
        cli_dedicated: Default dedicated flag from CLI (used if not specified in file)

    Returns:
        List of model spec dicts with checkpoint, revision, dedicated keys
    """
    path = Path(file_path)
    if not path.exists():
        raise click.ClickException(f"Config file not found: {file_path}")

    with open(path) as f:
        config = yaml.safe_load(f)

    if not config or "models" not in config:
        raise click.ClickException(f"Config file must contain a 'models' key: {file_path}")

    models = config["models"]
    if not isinstance(models, list):
        raise click.ClickException(f"'models' must be a list in {file_path}")

    specs = []
    for item in models:
        if isinstance(item, str):
            # Simple form: just the checkpoint string
            specs.append({
                "checkpoint": item,
                "revision": cli_revision,
                "dedicated": cli_dedicated,
            })
        elif isinstance(item, dict):
            # Full form: dict with checkpoint and optional revision/dedicated
            if "checkpoint" not in item:
                raise click.ClickException(f"Model entry missing 'checkpoint': {item}")
            specs.append({
                "checkpoint": item["checkpoint"],
                "revision": item.get("revision", cli_revision),
                "dedicated": item.get("dedicated", cli_dedicated),
            })
        else:
            raise click.ClickException(f"Invalid model entry (must be string or dict): {item}")

    return specs


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('-f', '--file', 'config_file', type=click.Path(), help='YAML config file with model specs')
@click.option('--revision', default=None, help='Model revision/branch (default: model\'s default)')
@click.option('--dedicated', is_flag=True, help='Deploy as dedicated - will not be evicted (default: False)')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def deploy(checkpoints: tuple, config_file: str, revision: str, dedicated: bool, ray_address: str, broker_url: str):
    """Deploy one or more models without requiring to submit a request.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Models can be specified as arguments or via a config file (-f).
    When using -f, per-model revision/dedicated settings in the file take precedence.

    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 meta-llama/Llama-3.1-8b
        ndif deploy gpt2 meta-llama/Llama-3.1-8b --dedicated
        ndif deploy -f models.yaml
        ndif deploy -f models.yaml --dedicated  # Sets default for models without dedicated specified
    """
    # Validate: must have either checkpoints or config file
    if not checkpoints and not config_file:
        raise click.ClickException("Must specify either CHECKPOINTS or --file/-f")

    # Use session defaults if not provided
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        # Check prerequisites silently
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)

        # Build model specs from checkpoints or config file
        if config_file:
            model_specs = _parse_model_specs(config_file, revision, dedicated)
            click.echo(f"Loaded {len(model_specs)} model(s) from {config_file}")
        else:
            # CLI checkpoints all share the same revision/dedicated
            model_specs = [
                {"checkpoint": cp, "revision": revision, "dedicated": dedicated}
                for cp in checkpoints
            ]

        # Group models by dedicated flag for batch deployment
        dedicated_models = [m for m in model_specs if m["dedicated"]]
        non_dedicated_models = [m for m in model_specs if not m["dedicated"]]

        # Generate model keys
        model_keys_map = {}  # model_key -> spec (for tracking dedicated flag)
        for spec in model_specs:
            click.echo(f"Generating model key for {spec['checkpoint']}{f' (revision: {spec['revision']})' if spec['revision'] else ''}...")
            model_key = get_model_key(spec["checkpoint"], spec["revision"])
            model_keys_map[model_key] = spec
            click.echo(f"  Model key: {model_key}")

        # Connect to Ray (suppress verbose output)
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Get controller actor handle
        controller = get_controller_actor_handle()

        # Deploy models in batches by dedicated flag
        all_evictions = []

        for is_dedicated, models in [(False, non_dedicated_models), (True, dedicated_models)]:
            if not models:
                continue

            batch_keys = [k for k, spec in model_keys_map.items() if spec["dedicated"] == is_dedicated]
            dedicated_label = " (dedicated)" if is_dedicated else ""
            click.echo(f"\nDeploying {len(batch_keys)} model(s){dedicated_label}...")

            object_ref = controller._deploy.remote(model_keys=batch_keys, dedicated=is_dedicated)
            results = ray.get(object_ref)

            # Collect evictions
            all_evictions.extend(results.get("evictions", []))

            # Report results for each model
            for model_key in batch_keys:
                result_status = results["result"].get(model_key, "UNKNOWN")

                if result_status == "CANT_ACCOMMODATE":
                    click.echo(f"  ✗ {model_key}: cannot be deployed on any node")
                elif result_status == "DEPLOYED":
                    click.echo(f"  ✓ {model_key}: already deployed")
                else:
                    click.echo(f"  ✓ {model_key}: deployed successfully")
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