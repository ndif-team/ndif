"""``ndif status`` — view cluster and deployment status."""

import json
import time
from collections import defaultdict

import click

from ..lib._common import NDIFConnectivityError, ensure_ray_connected


@click.command()
@click.option("--json-output", "json_flag", is_flag=True, help="Output raw JSON.")
@click.option("--verbose", is_flag=True, help="Dump the detailed cluster state.")
@click.option("--show-cold", is_flag=True, help="List all COLD deployments.")
@click.option("--watch", is_flag=True, help="Watch mode (refresh every 2s).")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
def status(json_flag, verbose, show_cold, watch, ray_address):
    """View cluster and deployment status.

    Shows deployments grouped by level (HOT/WARM/COLD) and cluster GPU resources.

    \b
    Examples:
        ndif status
        ndif status --show-cold
        ndif status --json-output
        ndif status --watch
    """
    try:
        ensure_ray_connected(ray_address)
    except NDIFConnectivityError as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()

    try:
        if watch:
            try:
                while True:
                    data = _fetch(verbose)
                    click.clear()
                    _render(data, json_flag, verbose, show_cold)
                    click.echo("\n(Press Ctrl+C to exit watch mode)")
                    time.sleep(2)
            except KeyboardInterrupt:
                click.echo("\nExiting watch mode...")
                return
        else:
            _render(_fetch(verbose), json_flag, verbose, show_cold)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _fetch(verbose: bool) -> dict:
    import ray

    from ...common.providers.ray import get_controller_actor_handle

    controller = get_controller_actor_handle()
    if verbose:
        return ray.get(controller.get_state.remote())
    return ray.get(controller.status.remote())


def _render(data: dict, json_flag: bool, verbose: bool, show_cold: bool) -> None:
    if json_flag or verbose:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        _render_simple(data, show_cold)


def _render_simple(data: dict, show_cold: bool) -> None:
    click.echo("NDIF Cluster Status")
    click.echo("=" * 60)
    click.echo()

    _render_resources(data.get("cluster", {}).get("nodes", {}))
    click.echo()

    by_level = defaultdict(list)
    for dep in data.get("deployments", {}).values():
        by_level[dep.get("deployment_level", "UNKNOWN")].append(dep)

    click.echo("Active Deployments:")
    click.echo()

    hot = by_level.get("HOT", [])
    click.echo(f"  🔥 HOT ({len(hot)})")
    for dep in hot:
        _print_deployment(dep)
    if not hot:
        click.echo("    (none)")
    click.echo()

    warm = by_level.get("WARM", [])
    click.echo(f"  🌡️  WARM ({len(warm)})")
    for dep in warm:
        _print_deployment(dep)
    if not warm:
        click.echo("    (none)")
    click.echo()

    cold = by_level.get("COLD", [])
    click.echo(f"  ❄️  COLD ({len(cold)})")
    if cold and show_cold:
        for dep in cold:
            _print_deployment(dep)
    elif cold:
        click.echo(f"    (use --show-cold to list all {len(cold)} models)")
    else:
        click.echo("    (none)")

    click.echo()
    click.echo("-" * 60)
    click.echo("Use 'ndif status --json-output' for raw data, '--verbose' for cluster state")


def _render_resources(nodes: dict) -> None:
    total_gpus = 0
    total_mem = 0
    free_mem = 0
    for node in nodes.values():
        gpus = node.get("resources", {}).get("gpu_details", [])
        total_gpus += len(gpus)
        total_mem += sum(g.get("memory_bytes", 0) for g in gpus)
        free_mem += sum(g.get("available_memory_bytes", 0) for g in gpus)

    click.echo("Cluster Resources:")
    click.echo(f"  Nodes: {len(nodes)}")
    click.echo(f"  Total GPUs: {total_gpus}")
    if total_mem:
        click.echo(f"  GPU Memory: {free_mem / 1024**3:.1f} / {total_mem / 1024**3:.1f} GB free")


def _print_deployment(dep: dict, indent: int = 4) -> None:
    spaces = " " * indent
    repo_id = dep.get("repo_id", "unknown")
    parts = []
    if dep.get("deployment_level") == "HOT" and dep.get("application_state"):
        parts.append(dep["application_state"])
    if dep.get("revision"):
        parts.append(f"rev: {dep['revision']}")
    n = dep.get("n_params")
    if n:
        if n >= 1_000_000_000:
            parts.append(f"{n / 1_000_000_000:.1f}B params")
        elif n >= 1_000_000:
            parts.append(f"{n / 1_000_000:.0f}M params")
        else:
            parts.append(f"{n:,} params")

    click.echo(f"{spaces}• {repo_id}")
    if parts:
        click.echo(f"{spaces}  {' | '.join(parts)}")
