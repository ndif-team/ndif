"""Status command for NDIF - view cluster and deployment status."""

import json
import time
import click
import ray
from collections import defaultdict

from .util import get_controller_actor_handle


@click.command()
@click.option('--json-output', 'json_flag', is_flag=True, help='Output raw JSON')
@click.option('--verbose', is_flag=True, help='Show detailed cluster state')
@click.option('--show-cold', is_flag=True, help='List all COLD deployments')
@click.option('--watch', is_flag=True, help='Watch mode (refresh every 2s)')
@click.option('--ray-address', default='ray://localhost:10001', help='Ray address (default: ray://localhost:10001)')
def status(json_flag: bool, verbose: bool, show_cold: bool, watch: bool, ray_address: str):
    """View cluster and deployment status.

    Shows current deployments grouped by level (HOT/WARM/COLD),
    cluster resources, and node information.

    Examples:
        ndif status                    # Quick overview
        ndif status --show-cold        # Include all COLD deployments
        ndif status --verbose          # Detailed cluster state
        ndif status --json-output      # Raw JSON output
        ndif status --watch            # Real-time monitoring
    """
    try:
        # Connect to Ray
        ray.init(address=ray_address, ignore_reinit_error=True)

        if watch:
            # Watch mode - loop forever
            try:
                while True:
                    # Clear screen
                    click.clear()
                    _display_status(json_flag, verbose, show_cold)
                    click.echo("\n(Press Ctrl+C to exit watch mode)")
                    time.sleep(2)
            except KeyboardInterrupt:
                click.echo("\nExiting watch mode...")
                return
        else:
            # Single shot
            _display_status(json_flag, verbose, show_cold)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _display_status(json_flag: bool, verbose: bool, show_cold: bool):
    """Display status with appropriate formatting."""
    controller = get_controller_actor_handle()

    if verbose:
        # Get detailed cluster state
        state_ref = controller.get_state.remote()
        state = ray.get(state_ref)

        if json_flag:
            click.echo(json.dumps(state, indent=2, default=str))
        else:
            format_state_verbose(state)
    else:
        # Get high-level status
        status_ref = controller.status.remote()
        status_data = ray.get(status_ref)

        if json_flag:
            click.echo(json.dumps(status_data, indent=2, default=str))
        else:
            format_status_simple(status_data, show_cold)


def format_status_simple(status_data: dict, show_cold: bool):
    """Pretty-print status overview."""

    click.echo("NDIF Cluster Status")
    click.echo("=" * 60)
    click.echo()

    # Cluster resources summary
    cluster_info = status_data.get('cluster', {})
    nodes = cluster_info.get('nodes', {})

    total_gpus = 0
    available_gpus = 0
    total_gpu_memory = 0

    for node_data in nodes.values():
        resources = node_data.get('resources', {})
        total_gpus += int(resources.get('total_gpus', 0))
        available_gpus += len(resources.get('available_gpus', []))
        total_gpu_memory += resources.get('gpu_memory_bytes', 0)

    click.echo("Cluster Resources:")
    click.echo(f"  Nodes: {len(nodes)}")
    click.echo(f"  Total GPUs: {total_gpus} ({available_gpus} available)")
    if total_gpu_memory > 0:
        click.echo(f"  GPU Memory: {total_gpu_memory / (1024**3):.1f} GB per GPU")
    click.echo()

    # Group deployments by level
    deployments = status_data.get('deployments', {})
    by_level = defaultdict(list)

    for actor_name, deployment_info in deployments.items():
        level = deployment_info.get('deployment_level', 'UNKNOWN')
        by_level[level].append(deployment_info)

    # Active Deployments
    click.echo("Active Deployments:")
    click.echo()

    # HOT deployments
    hot_deployments = by_level.get('HOT', [])
    click.echo(f"  🔥 HOT ({len(hot_deployments)})")
    if hot_deployments:
        for dep in hot_deployments:
            _print_deployment(dep, indent=4)
    else:
        click.echo("    (none)")
    click.echo()

    # WARM deployments
    warm_deployments = by_level.get('WARM', [])
    click.echo(f"  🌡️  WARM ({len(warm_deployments)})")
    if warm_deployments:
        for dep in warm_deployments:
            _print_deployment(dep, indent=4)
    else:
        click.echo("    (none)")
    click.echo()

    # COLD deployments
    cold_deployments = by_level.get('COLD', [])
    click.echo(f"  ❄️  COLD ({len(cold_deployments)})")
    if show_cold and cold_deployments:
        for dep in cold_deployments:
            _print_deployment(dep, indent=4)
    else:
        if cold_deployments:
            click.echo(f"    (use --show-cold to list all {len(cold_deployments)} models)")
        else:
            click.echo("    (none)")

    click.echo()
    click.echo("-" * 60)
    click.echo("Use 'ndif status --json-output' for raw data")
    click.echo("Use 'ndif status --verbose' for detailed cluster state")


def _print_deployment(dep: dict, indent: int = 0):
    """Print a single deployment's info."""
    spaces = " " * indent
    repo_id = dep.get('repo_id', 'unknown')
    revision = dep.get('revision')
    n_params = dep.get('n_params')
    application_state = dep.get('application_state')
    deployment_level = dep.get('deployment_level')

    # Build info line
    info_parts = []

    # Show application state for HOT deployments
    if deployment_level == 'HOT' and application_state:
        info_parts.append(application_state)

    if revision:
        info_parts.append(f"rev: {revision}")
    if n_params:
        # Format params nicely (M for million, B for billion)
        if n_params >= 1_000_000_000:
            info_parts.append(f"{n_params / 1_000_000_000:.1f}B params")
        elif n_params >= 1_000_000:
            info_parts.append(f"{n_params / 1_000_000:.0f}M params")
        else:
            info_parts.append(f"{n_params:,} params")

    if info_parts:
        click.echo(f"{spaces}• {repo_id}")
        click.echo(f"{spaces}  {' | '.join(info_parts)}")
    else:
        click.echo(f"{spaces}• {repo_id}")


def format_state_verbose(state: dict):
    """Pretty-print detailed cluster state."""

    click.echo("NDIF Detailed Cluster State")
    click.echo("=" * 60)
    click.echo()

    # Metadata
    cluster_data = state.get('cluster', {})
    metadata = {
        'datetime': state.get('datetime'),
        'execution_timeout_seconds': state.get('execution_timeout_seconds'),
        'model_cache_percentage': state.get('model_cache_percentage'),
        'minimum_deployment_time_seconds': state.get('minimum_deployment_time_seconds'),
    }

    click.echo("Configuration:")
    for key, value in metadata.items():
        if value is not None:
            click.echo(f"  {key}: {value}")
    click.echo()

    # Nodes
    nodes = cluster_data.get('nodes', [])
    click.echo(f"Nodes ({len(nodes)}):")
    click.echo()

    for node in nodes:
        click.echo(f"  Node: {node.get('name', 'unknown')}")
        click.echo(f"    ID: {node.get('id', 'unknown')[:16]}...")

        resources = node.get('resources', {})
        click.echo("    Resources:")
        click.echo(f"      Total GPUs: {resources.get('total_gpus', 0)}")
        click.echo(f"      Available GPUs: {resources.get('available_gpus', [])}")
        click.echo(f"      GPU Type: {resources.get('gpu_type', 'unknown')}")
        click.echo(f"      GPU Memory: {resources.get('gpu_memory_bytes', 0) / (1024**3):.1f} GB")
        click.echo(f"      CPU Memory Total: {resources.get('cpu_memory_bytes', 0) / (1024**3):.1f} GB")
        click.echo(f"      CPU Memory Available: {resources.get('available_cpu_memory_bytes', 0) / (1024**3):.1f} GB")

        deployments = node.get('deployments', [])
        click.echo(f"    Deployments ({node.get('num_deployments', 0)}):")
        if deployments:
            for dep in deployments:
                repo_id = _extract_repo_id_from_model_key(dep.get('model_key', ''))
                click.echo(f"      • {repo_id}")
                click.echo(f"        Level: {dep.get('deployment_level', 'unknown')}")
                click.echo(f"        GPUs: {dep.get('gpus', [])}")
                click.echo(f"        Size: {dep.get('size_bytes', 0) / (1024**3):.2f} GB")
                click.echo(f"        Dedicated: {dep.get('dedicated', False)}")
        else:
            click.echo("      (none)")

        cache = node.get('cache', [])
        cache_size = node.get('cache_size', 0)
        click.echo(f"    Cache ({len(cache)} models, {cache_size / (1024**3):.2f} GB):")
        if cache:
            for cached in cache:
                repo_id = _extract_repo_id_from_model_key(cached.get('model_key', ''))
                click.echo(f"      • {repo_id}")
                click.echo(f"        Size: {cached.get('size_bytes', 0) / (1024**3):.2f} GB")
        else:
            click.echo("      (empty)")

        click.echo()

    # Evaluator cache
    evaluator = state.get('evaluator', {})
    eval_cache = evaluator.get('cache', {})
    click.echo(f"Evaluator Cache ({len(eval_cache)} models):")
    if eval_cache:
        for model_key, model_info in list(eval_cache.items())[:5]:  # Show first 5
            repo_id = _extract_repo_id_from_model_key(model_key)
            size_bytes = model_info.get('size_in_bytes', 0)
            click.echo(f"  • {repo_id}: {size_bytes / (1024**3):.2f} GB")
        if len(eval_cache) > 5:
            click.echo(f"  ... and {len(eval_cache) - 5} more")
    else:
        click.echo("  (empty)")

    click.echo()
    click.echo("-" * 60)


def _extract_repo_id_from_model_key(model_key: str) -> str:
    """Extract repo_id from model_key string."""
    # model_key format: 'nnsight.modeling.language.LanguageModel:{"repo_id": "...", ...}'
    try:
        if '"repo_id":' in model_key:
            start = model_key.index('"repo_id":') + len('"repo_id":')
            remainder = model_key[start:].strip()
            if remainder.startswith('"'):
                end = remainder.index('"', 1)
                return remainder[1:end]
    except (ValueError, IndexError):
        pass
    return model_key
