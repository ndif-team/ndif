"""Export command for NDIF - export current deployments to a config file."""

import click
import ray
import yaml
from pathlib import Path

from ..lib.util import get_current_deployments
from ..lib.checks import check_prerequisites
from ..lib.session import get_env
from ..lib.model_config import save_model_config


@click.command()
@click.option('-f', '--file', 'output_file', type=click.Path(), default=None,
              help='Output file path')
@click.option('--stdout', 'to_stdout', is_flag=True, help='Print to stdout instead of file')
@click.option('--ray-address', default=None, help='Ray address (default: from NDIF_RAY_ADDRESS)')
def export(output_file: str, to_stdout: bool, ray_address: str):
    """Export current HOT deployments to a YAML config file.

    Saves the current state of deployed models so you can restore it later
    with 'ndif deploy -f <file>'.

    \b
    Examples:
        ndif export -f models.yaml      # Export to file
        ndif export --stdout            # Print to stdout
        ndif export --stdout > m.yaml   # Redirect to file
    """
    if not output_file and not to_stdout:
        raise click.ClickException("Must specify either --file/-f or --stdout")

    if output_file and to_stdout:
        raise click.ClickException("Cannot use both --file/-f and --stdout")

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")

    try:
        check_prerequisites(ray_address=ray_address)

        if not to_stdout:
            click.echo(f"Connecting to Ray at {ray_address}...")
        ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")

        # Fetch current HOT deployments
        hot_deployments = get_current_deployments(level="HOT")

        if not hot_deployments:
            if to_stdout:
                click.echo("models: []")
            else:
                click.echo("No HOT deployments to export.")
            return

        if to_stdout:
            # Build and print YAML to stdout
            models = _build_models_list(hot_deployments)
            click.echo(yaml.dump({"models": models}, default_flow_style=False, sort_keys=False))
        else:
            # Save to file
            output_path = Path(output_file)
            save_model_config(output_path, hot_deployments)

            click.echo(f"Exported {len(hot_deployments)} deployment(s) to {output_path}")
            click.echo()
            click.echo("Models:")
            for dep in hot_deployments:
                repo_id = dep.get("repo_id", "unknown")
                dedicated = dep.get("dedicated", False)
                revision = dep.get("revision")
                extras = []
                if dedicated:
                    extras.append("dedicated")
                if revision:
                    extras.append(f"rev: {revision}")
                extra_str = f" ({', '.join(extras)})" if extras else ""
                click.echo(f"  - {repo_id}{extra_str}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _build_models_list(deployments: list[dict]) -> list:
    """Build models list for YAML output."""
    models = []
    for dep in deployments:
        repo_id = dep.get("repo_id") or dep.get("checkpoint")
        revision = dep.get("revision")
        dedicated = dep.get("dedicated", False)

        if not revision and not dedicated:
            models.append(repo_id)
        else:
            entry = {"checkpoint": repo_id}
            if revision:
                entry["revision"] = revision
            if dedicated:
                entry["dedicated"] = dedicated
            models.append(entry)
    return models
