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

        # Fetch current HOT deployments (one entry per replica with the new
        # controller status — aggregate to one entry per model_key with the
        # replica count).
        hot_replicas = get_current_deployments(level="HOT")
        hot_deployments = _aggregate_by_model_key(hot_replicas)

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

            click.echo(f"Exported {len(hot_deployments)} model(s) to {output_path}")
            click.echo()
            click.echo("Models:")
            for dep in hot_deployments:
                repo_id = dep.get("repo_id", "unknown")
                pinned = dep.get("pinned", False)
                replicas = dep.get("replicas", 1)
                revision = dep.get("revision")
                extras = []
                if pinned:
                    extras.append("pinned")
                if replicas != 1:
                    extras.append(f"replicas: {replicas}")
                if revision:
                    extras.append(f"rev: {revision}")
                extra_str = f" ({', '.join(extras)})" if extras else ""
                click.echo(f"  - {repo_id}{extra_str}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _aggregate_by_model_key(replicas: list[dict]) -> list[dict]:
    """Collapse a per-replica deployment list into one entry per model_key.

    Replica-level fields (replica_id, gpus, schedule end_time) are dropped;
    the model-level fields (repo_id, revision, pinned, actor_class) come
    from the first replica encountered (they should be uniform across
    siblings). ``replicas`` is the count of HOT replicas for that model.
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
            }
            counts[mk] = 0
        counts[mk] += 1
    for mk, entry in by_mk.items():
        entry["replicas"] = counts[mk]
    return list(by_mk.values())


def _build_models_list(deployments: list[dict]) -> list:
    """Build models list for YAML output."""
    models = []
    for dep in deployments:
        repo_id = dep.get("repo_id") or dep.get("checkpoint")
        revision = dep.get("revision")
        pinned = dep.get("pinned", False)
        replicas = int(dep.get("replicas", 1) or 1)
        actor_class = dep.get("actor_class")

        if not revision and not pinned and replicas == 1 and not actor_class:
            models.append(repo_id)
        else:
            entry = {"checkpoint": repo_id}
            if revision:
                entry["revision"] = revision
            if pinned:
                entry["pinned"] = pinned
            if replicas != 1:
                entry["replicas"] = replicas
            if actor_class:
                entry["actor_class"] = actor_class
            models.append(entry)
    return models
