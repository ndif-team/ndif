"""``ndif export`` — export current HOT deployments to a YAML config file."""

from pathlib import Path

import click
import yaml

from ..lib._common import NDIFConnectivityError, ensure_ray_connected
from ..lib.model_config import build_models_list, save_model_config
from ..lib.models import get_current_deployments


@click.command()
@click.option("-f", "--file", "output_file", type=click.Path(), default=None,
              help="Output file path.")
@click.option("--stdout", "to_stdout", is_flag=True, help="Print to stdout instead of a file.")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
def export(output_file, to_stdout, ray_address):
    """Export current HOT deployments to a YAML config file.

    Saves the current state so you can restore it later with ``ndif deploy -f``.

    \b
    Examples:
        ndif export -f models.yaml
        ndif export --stdout
        ndif export --stdout > my-setup.yaml
    """
    if not output_file and not to_stdout:
        raise click.ClickException("Must specify either --file/-f or --stdout")
    if output_file and to_stdout:
        raise click.ClickException("Cannot use both --file/-f and --stdout")

    try:
        ensure_ray_connected(ray_address)
    except NDIFConnectivityError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()

    try:
        hot_deployments = _aggregate_by_model_key(get_current_deployments(level="HOT"))

        if not hot_deployments:
            click.echo("models: []" if to_stdout else "No HOT deployments to export.")
            return

        if to_stdout:
            models = build_models_list(hot_deployments)
            click.echo(yaml.dump({"models": models}, default_flow_style=False, sort_keys=False))
            return

        output_path = Path(output_file)
        save_model_config(output_path, hot_deployments)
        click.echo(f"Exported {len(hot_deployments)} model(s) to {output_path}")
        click.echo()
        click.echo("Models:")
        for dep in hot_deployments:
            extras = []
            if dep.get("pinned"):
                extras.append("pinned")
            if dep.get("replicas", 1) != 1:
                extras.append(f"replicas: {dep['replicas']}")
            if dep.get("revision"):
                extras.append(f"rev: {dep['revision']}")
            suffix = f" ({', '.join(extras)})" if extras else ""
            click.echo(f"  - {dep.get('repo_id', 'unknown')}{suffix}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _aggregate_by_model_key(replicas: list[dict]) -> list[dict]:
    """Collapse a per-replica deployment list into one entry per model_key.

    Carries every deployment field ``controller.status()`` reports that
    ``deploy -f`` can set again, so an exported config restores the deployment
    as it was. ``envoy_class`` needs no entry of its own — it is the prefix of
    ``model_key``, which is carried. ``padding_factor`` is not recoverable: it
    lives on the deploy-time config, not on the deployment.
    """
    by_mk: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for r in replicas:
        mk = r.get("model_key")
        if not mk:
            continue
        if mk not in by_mk:
            by_mk[mk] = {
                "model_key": mk,
                "repo_id": r.get("repo_id") or r.get("checkpoint"),
                "revision": r.get("revision"),
                "pinned": bool(r.get("pinned", False)),
                "actor_class": r.get("actor_class"),
                "trusted": bool(r.get("trusted", False)),
                "dtype": r.get("dtype"),
                "execution_timeout_seconds": r.get("execution_timeout_seconds"),
            }
            counts[mk] = 0
        counts[mk] += 1
    for mk, entry in by_mk.items():
        entry["replicas"] = counts[mk]
    return list(by_mk.values())
