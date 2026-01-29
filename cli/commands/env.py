"""Env command for NDIF - view Ray cluster environment information."""

import json
import pickle
import click

from ..lib.session import get_current_session, get_env
from ..lib.checks import check_redis

# Key packages from pyproject.toml dependencies
KEY_PACKAGES = [
    'fastapi',
    'python-socketio',
    'redis',
    'uvicorn',
    'gunicorn',
    'ray',
    'nnsight',
    'boto3',
    'torch',
    'transformers',
    'numpy',
]


@click.command()
@click.option('--json-output', 'json_flag', is_flag=True, help='Output as JSON')
@click.option('--all', 'show_all', is_flag=True, help='Show all installed packages (default: key packages only)')
@click.option('--local', is_flag=True, help='Show local system info instead of cluster env')
def env(json_flag: bool, show_all: bool, local: bool):
    """Show Ray cluster environment information.

    Displays Python version and installed packages from the Ray cluster,
    as cached in Redis by the API service.

    Examples:
        ndif env                   # Show key packages only
        ndif env --all             # Show all installed packages
        ndif env --json-output     # Output as JSON
        ndif env --local           # Show local system info
    """
    if local:
        _show_local_env(json_flag)
        return

    # Get broker URL from session or defaults
    session = get_current_session()
    broker_url = session.config.broker_url if session else get_env("NDIF_BROKER_URL")

    # Check if Redis is reachable
    if not check_redis(broker_url):
        click.echo(f"Error: Cannot reach broker at {broker_url}", err=True)
        click.echo("Make sure the broker is running (ndif start broker)", err=True)
        click.echo("\nUse --local to show local system info instead.", err=True)
        return

    # Try to get cached env from Redis
    try:
        import redis as redis_sync
        client = redis_sync.Redis.from_url(broker_url, socket_connect_timeout=2)
        cached_env = client.get("env")
        client.close()

        if cached_env is None:
            click.echo("No environment info cached in Redis.")
            click.echo("The API service caches this after the first /env request.")
            click.echo("\nUse --local to show local system info instead.")
            return

        env_data = pickle.loads(cached_env)

        if json_flag:
            click.echo(json.dumps(env_data, indent=2))
        else:
            _format_env_human(env_data, show_all)

    except Exception as e:
        click.echo(f"Error reading environment info: {e}", err=True)


def _format_env_human(env_data: dict, show_all: bool = False):
    """Format environment data for human readability."""
    click.echo("Ray Cluster Environment")
    click.echo("=" * 60)
    click.echo()

    # Python version
    python_version = env_data.get('python_version', 'unknown')
    click.echo(f"Python Version: {python_version}")
    click.echo()

    # Installed packages (dict: name -> version)
    all_packages = env_data.get('packages', {})

    if show_all:
        packages = all_packages
        click.echo(f"All Installed Packages ({len(packages)}):")
    else:
        # Filter to key packages only
        packages = {k: v for k, v in all_packages.items() if k.lower() in [p.lower() for p in KEY_PACKAGES]}
        click.echo(f"Key Packages ({len(packages)}/{len(all_packages)} total):")

    if packages:
        click.echo("-" * 40)

        # Sort packages alphabetically
        for name in sorted(packages.keys(), key=str.lower):
            version = packages[name]
            click.echo(f"  {name}=={version}")

        if not show_all:
            click.echo()
            click.echo("Use --all to show all installed packages.")
    else:
        click.echo("No package information available.")


def _show_local_env(json_flag: bool):
    """Show local system environment info."""
    import sys
    import platform

    data = {
        'python_version': sys.version,
        'platform': platform.platform(),
        'machine': platform.machine(),
        'processor': platform.processor(),
    }

    # Try to get GPU info
    try:
        import subprocess
        result = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader'],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            gpus = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    gpus.append(line.strip())
            data['gpus'] = gpus
    except FileNotFoundError:
        pass

    # Try to get key packages
    try:
        import importlib.metadata
        key_packages = ['torch', 'ray', 'nnsight', 'transformers', 'numpy']
        installed = {}
        for pkg in key_packages:
            try:
                version = importlib.metadata.version(pkg)
                installed[pkg] = version
            except importlib.metadata.PackageNotFoundError:
                pass
        data['key_packages'] = installed
    except ImportError:
        pass

    if json_flag:
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo("Local System Environment")
        click.echo("=" * 60)
        click.echo()
        click.echo(f"Python: {data['python_version'].split()[0]}")
        click.echo(f"Platform: {data['platform']}")
        click.echo(f"Machine: {data['machine']}")
        click.echo()

        if 'gpus' in data:
            click.echo("GPUs:")
            for gpu in data['gpus']:
                click.echo(f"  {gpu}")
            click.echo()

        if 'key_packages' in data:
            click.echo("Key Packages:")
            for name, version in data['key_packages'].items():
                click.echo(f"  {name}=={version}")
