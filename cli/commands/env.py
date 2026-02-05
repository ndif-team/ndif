"""Env command for NDIF - view Ray cluster environment information."""

import json
import pickle
import time
import click
import importlib.metadata
import subprocess
import sys
import platform
import requests
import redis as redis_sync

from ..lib.session import get_current_session, get_env
from ..lib.checks import check_redis, check_api

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
@click.option('--broker-url', default=None, help='Broker URL (default: from NDIF_BROKER_URL)')
def env(json_flag: bool, show_all: bool, local: bool, broker_url: str):
    """Show Ray cluster environment information.

    Displays Python version and installed packages from the Ray cluster,
    as cached in Redis by the API service.

    Examples:
        ndif env                   # Show key packages only
        ndif env --all             # Show all installed packages
        ndif env --json-output     # Output as JSON
        ndif env --local           # Show local system info
        ndif env --broker-url redis://host:6379  # Query specific broker
    """
    if local:
        _show_local_env(json_flag)
        return

    # Get broker URL and API URL: CLI arg > session > env default
    session = get_current_session()
    if broker_url is None:
        broker_url = session.config.broker_url if session else get_env("NDIF_BROKER_URL")
    api_url = session.config.api_url if session else get_env("NDIF_API_URL")

    # Check if Redis is reachable
    if not check_redis(broker_url):
        click.echo(f"Error: Cannot reach broker at {broker_url}", err=True)
        click.echo("Make sure the broker is running (ndif start broker)", err=True)
        click.echo("\nUse --local to show local system info instead.", err=True)
        return

    # Try to get cached env from Redis
    try:
        client = redis_sync.Redis.from_url(broker_url, socket_connect_timeout=2)
        cached_env = client.get("env")

        # If not cached, trigger the API to populate it
        if cached_env is None:
            client.close()
            cached_env = _fetch_and_cache_env(api_url, broker_url)

        else:
            client.close()

        if cached_env is None:
            click.echo("No environment info available.")
            click.echo("\nUse --local to show local system info instead.")
            return

        env_data = pickle.loads(cached_env)

        if json_flag:
            click.echo(json.dumps(env_data, indent=2))
        else:
            _format_env_human(env_data, show_all)

    except Exception as e:
        click.echo(f"Error reading environment info: {e}", err=True)


def _fetch_and_cache_env(api_url: str, broker_url: str, timeout: int = 30) -> bytes | None:
    """Request /env from the API to populate the cache, then return cached data.

    Args:
        api_url: API base URL
        broker_url: Redis broker URL
        timeout: Maximum seconds to wait for cache to populate

    Returns:
        Cached env data as bytes, or None if failed
    """
    # Check if API is reachable
    if not check_api(api_url):
        click.echo(f"Error: Cannot reach API at {api_url}", err=True)
        click.echo("Make sure the API service is running (ndif start api)", err=True)
        return None

    # Make the /env request to trigger caching
    click.echo("Fetching environment info from cluster...")
    try:
        response = requests.get(f"{api_url}/env", timeout=timeout)
        if response.status_code != 200:
            click.echo(f"Error: API returned status {response.status_code}", err=True)
            return None
    except requests.RequestException as e:
        click.echo(f"Error fetching from API: {e}", err=True)
        return None

    # Wait briefly for cache to populate, then read from Redis
    time.sleep(0.5)

    try:
        client = redis_sync.Redis.from_url(broker_url, socket_connect_timeout=2)
        cached_env = client.get("env")
        client.close()
        return cached_env
    except Exception as e:
        click.echo(f"Error reading from cache: {e}", err=True)
        return None


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

    data = {
        'python_version': sys.version,
        'platform': platform.platform(),
        'machine': platform.machine(),
        'processor': platform.processor(),
    }

    # Try to get GPU info
    try:
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
