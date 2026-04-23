"""Deploy command for NDIF - Deploy a model without requiring to submit a request."""

import click
import ray
import asyncio
from pathlib import Path

from src.common.schema.deployment_config import DeploymentConfig
from ..lib.util import get_controller_actor_handle, get_model_key, notify_dispatcher, get_current_deployments, wait_for_model_ready
from ..lib.checks import check_prerequisites
from ..lib.session import get_env
from ..lib.model_config import load_model_config


@click.command()
@click.argument('checkpoints', nargs=-1)
@click.option('-f', '--file', 'config_file', type=click.Path(), help='YAML config file with model specs')
@click.option('--sync', is_flag=True, help='Sync mode: evict models not in config file (requires -f)')
@click.option('--revision', default=None, help='Model revision/branch (default: model\'s default)')
@click.option('--dedicated', is_flag=True, help='Deploy as dedicated - will not be evicted (default: False)')
@click.option('--actor-class', 'actor_class', default=None,
              help='Dotted import path of the Ray actor class to use (default: ModelActor). '
                   'With -f, acts as the default for entries that do not set actor_class themselves.')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def deploy(checkpoints: tuple, config_file: str, sync: bool, revision: str, dedicated: bool, actor_class: str, ray_address: str, broker_url: str):
    """Deploy one or more models without requiring to submit a request.

    CHECKPOINTS: One or more model checkpoints (e.g., "gpt2", "meta-llama/Llama-2-7b-hf")

    Models can be specified as arguments or via a config file (-f).
    When using -f, per-model revision/dedicated settings in the file take precedence.

    Use --sync with -f to make the cluster state match the config file exactly,
    evicting any models not specified in the file.

    \b
    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 meta-llama/Llama-3.1-8b
        ndif deploy gpt2 --dedicated
        ndif deploy -f models.yaml
        ndif deploy -f models.yaml --sync    # Evict models not in file
    """
    # Validate arguments
    if not checkpoints and not config_file:
        raise click.ClickException("Must specify either CHECKPOINTS or --file/-f")

    if sync and not config_file:
        raise click.ClickException("--sync requires --file/-f")

    if sync and checkpoints:
        raise click.ClickException("--sync cannot be used with checkpoint arguments")

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)

        # Build model specs from checkpoints or config file
        if config_file:
            try:
                model_specs = load_model_config(
                    Path(config_file),
                    default_revision=revision,
                    default_dedicated=dedicated,
                    default_actor_class=actor_class,
                )
            except (FileNotFoundError, ValueError) as e:
                raise click.ClickException(str(e))
            click.echo(f"Loaded {len(model_specs)} model(s) from {config_file}")
        else:
            model_specs = [
                {"checkpoint": cp, "revision": revision, "dedicated": dedicated, "actor_class": actor_class}
                for cp in checkpoints
            ]

        # Generate model keys
        model_keys_map = {}  # model_key -> spec
        for spec in model_specs:
            click.echo(f"Generating model key for {spec['checkpoint']}{f' (revision: {spec['revision']})' if spec['revision'] else ''}...")
            model_key = get_model_key(spec["checkpoint"], spec["revision"])
            model_keys_map[model_key] = spec
            click.echo(f"  Model key: {model_key}")

        # Connect to Ray
        click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        controller = get_controller_actor_handle()

        # In sync mode, evict models not in the config file first
        if sync:
            _sync_evict_extra_models(controller, model_keys_map, broker_url)

        # Deploy models in batches by dedicated flag
        all_evictions = []
        dedicated_models = [m for m in model_specs if m["dedicated"]]
        non_dedicated_models = [m for m in model_specs if not m["dedicated"]]

        for is_dedicated, models in [(False, non_dedicated_models), (True, dedicated_models)]:
            if not models:
                continue

            batch_keys = [k for k, spec in model_keys_map.items() if spec["dedicated"] == is_dedicated]
            dedicated_label = " (dedicated)" if is_dedicated else ""
            click.echo(f"\nDeploying {len(batch_keys)} model(s){dedicated_label}...")

            configs = {
                k: DeploymentConfig(
                    dedicated=is_dedicated,
                    actor_class=model_keys_map[k].get("actor_class"),
                )
                for k in batch_keys
            }
            object_ref = controller._deploy.remote(deployments=configs)
            results = ray.get(object_ref)

            all_evictions.extend(results.get("evictions", []))

            # Track which models need initialization
            models_to_init = []

            for model_key in batch_keys:
                result_status = results["result"].get(model_key, "UNKNOWN")

                if result_status == "CANT_ACCOMMODATE":
                    click.echo(f"  ✗ {model_key}: cannot be deployed on any node")
                elif result_status == "DEPLOYED":
                    click.echo(f"  ✓ {model_key}: already deployed")
                elif result_status in ("FREE", "CACHED_AND_FREE"):
                    click.echo(f"  ⋯ {model_key}: provisioned, initializing...")
                    models_to_init.append(model_key)
                elif result_status in ("FULL", "CACHED_AND_FULL"):
                    click.echo(f"  ✗ {model_key}: no capacity available")
                else:
                    click.echo(f"  ? {model_key}: unknown status ({result_status})")

            # Wait for models to initialize
            for model_key in models_to_init:
                try:
                    if wait_for_model_ready(model_key):
                        click.echo(f"  ✓ {model_key}: ready")
                        asyncio.run(notify_dispatcher(broker_url, "deploy", model_key))
                    else:
                        click.echo(f"  ✗ {model_key}: initialization timed out")
                except Exception as e:
                    click.echo(f"  ✗ {model_key}: initialization failed - {e}")

        if all_evictions:
            click.echo("\nEvictions:")
            for eviction in all_evictions:
                click.echo(f"  - {eviction}")
                asyncio.run(notify_dispatcher(broker_url, "evict", eviction))

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()


def _sync_evict_extra_models(controller, desired_keys: dict, broker_url: str):
    """Evict HOT models that are not in the desired set.

    Args:
        controller: Ray controller actor handle
        desired_keys: Dict of model_key -> spec for models that should be deployed
        broker_url: Broker URL for dispatcher notification
    """
    current_deployments = get_current_deployments(level="HOT")
    current_keys = {dep["model_key"] for dep in current_deployments}
    desired_key_set = set(desired_keys.keys())

    to_evict = current_keys - desired_key_set

    if not to_evict:
        return

    click.echo(f"\nSync mode: evicting {len(to_evict)} model(s) not in config...")

    object_ref = controller.evict.remote(model_keys=list(to_evict))
    results = ray.get(object_ref)

    for model_key, result in results.items():
        if result["status"] == "not_found":
            click.echo(f"  - {model_key}: not found")
        else:
            click.echo(f"  ✓ {model_key}: evicted")
            asyncio.run(notify_dispatcher(broker_url, "evict", model_key))
