"""Deploy command for NDIF - Deploy one or more models to the cluster."""

import asyncio
from pathlib import Path
from typing import Tuple

import click
import ray
import yaml

from ..lib.util import get_controller_actor_handle, notify_dispatcher
from ..lib.checks import check_prerequisites
from ..lib.session import get_env
from src.common.schema import DeploymentConfig


def _load_yaml_config(path: Path) -> dict:
    """Load a YAML deployment config file."""
    text = path.read_text()
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise click.ClickException(f"Config file must be a YAML mapping, got {type(data).__name__}")
    return data


@click.command()
@click.argument("checkpoints", nargs=-1, required=True)
@click.option(
    "--deployment-config",
    "config_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to YAML file with per-model deployment settings",
)
@click.option(
    "--dedicated",
    is_flag=True,
    help="Override all models to dedicated (will not be evicted from hotswapping)",
)
@click.option("--ray-address", default=None, help="Ray address (default: from NDIF_RAY_ADDRESS)")
@click.option("--broker-url", default=None, help="Broker URL (default: from NDIF_BROKER_URL)")
def deploy(
    checkpoints: Tuple[str, ...],
    config_path: Path | None,
    dedicated: bool,
    ray_address: str,
    broker_url: str,
):
    """Deploy one or more models to the cluster.

    CHECKPOINTS: One or more model checkpoints (e.g. "gpt2", "meta-llama/Llama-2-7b-hf").

    Optionally provide a YAML config file with per-model DeploymentConfig overrides.
    Models on the CLI that are not in the config get default settings.
    Models in the config that are not on the CLI are ignored.

    Config file format (YAML):
    ```yaml
    gpt2:
        device_map: cpu
        dedicated: true
        revision: main
    meta-llama/Llama-2-7b-hf:
        padding_factor: 0.2
    ```

    Examples:
        ndif deploy gpt2
        ndif deploy gpt2 meta-llama/Llama-2-7b-hf
        ndif deploy gpt2 --deployment-config config.yaml
        ndif deploy gpt2 llama --deployment-config config.yaml --dedicated
    """
    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")

    try:
        check_prerequisites(broker_url=broker_url, ray_address=ray_address)

        config = _load_yaml_config(config_path) if config_path else {}
        models = [DeploymentConfig(model_key=checkpoint, **config.get(checkpoint, {})) for checkpoint in checkpoints]
        if dedicated:
            for model in models:
                model.dedicated = True

        click.echo(f"Deploying {len(models)} model(s)...")

        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")
        controller = get_controller_actor_handle()

        object_ref = controller._deploy.remote(models)
        results = ray.get(object_ref)
        result_map = results["result"]
        evictions = results.get("evictions", set())

        all_ok = True
        # Print the results of the deployment
        for model_key, status in result_map.items():
            if status == "CANT_ACCOMMODATE":
                click.echo(f"✗ {model_key}: cannot accommodate on any node")
                all_ok = False
            else:
                click.echo(f"✓ {model_key}: deployed ({status})")
                # Notify the dispatcher of the deployment
                asyncio.run(notify_dispatcher(broker_url, "deploy", model_key))

        # Notify the dispatcher of the evictions
        if evictions:
            click.echo("• Evictions:")
            for ev in evictions:
                click.echo(f"  - {ev}")
                asyncio.run(notify_dispatcher(broker_url, "evict", ev))

        # Notify the user if one or more models could not be deployed
        if not all_ok:
            click.echo("One or more models could not be deployed", err=True)

    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()