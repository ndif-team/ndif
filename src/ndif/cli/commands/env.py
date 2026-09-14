"""``ndif env`` — show the Ray cluster's Python/package environment.

Fetches the cluster env from the API's ``/env`` endpoint (served from a
Redis-cached, coalesced refresh the dispatcher populates). Use ``--local`` to
show this machine's environment instead — handy for spotting version drift
between a client and the cluster.
"""

import json
import platform
import subprocess
import sys
import urllib.error
import urllib.request

import click

from .. import config

# Packages worth surfacing by default (matched against import names).
KEY_PACKAGES = [
    "fastapi", "uvicorn", "gunicorn", "redis", "ray", "nnsight",
    "boto3", "torch", "transformers", "numpy", "pydantic",
]


@click.command()
@click.option("--json-output", "json_flag", is_flag=True, help="Output as JSON.")
@click.option("--all", "show_all", is_flag=True, help="Show all packages (default: key packages).")
@click.option("--local", is_flag=True, help="Show this machine's environment instead of the cluster's.")
@click.option("--api-url", default=None, help="API URL (default: NDIF_API_URL).")
def env(json_flag, show_all, local, api_url):
    """Show the Ray cluster's Python version and installed packages.

    \b
    Examples:
        ndif env
        ndif env --all
        ndif env --json-output
        ndif env --local
    """
    if local:
        _show_local_env(json_flag)
        return

    api_url = api_url or config.get("NDIF_API_URL")
    try:
        with urllib.request.urlopen(f"{api_url.rstrip('/')}/env", timeout=60) as resp:
            env_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        click.echo(f"Error: API returned {e.code} fetching cluster env from {api_url}", err=True)
        click.echo("The cluster may be down. Use --local to show this machine instead.", err=True)
        raise click.Abort()
    except (urllib.error.URLError, OSError) as e:
        click.echo(f"Error: cannot reach API at {api_url}: {e}", err=True)
        click.echo("Start the API (ndif start api). Use --local to show this machine instead.", err=True)
        raise click.Abort()

    if json_flag:
        click.echo(json.dumps(env_data, indent=2))
    else:
        _format_env(env_data, show_all)


def _format_env(env_data: dict, show_all: bool) -> None:
    click.echo("Ray Cluster Environment")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"Python: {env_data.get('python_version', 'unknown').split()[0]}")
    click.echo()

    all_packages = env_data.get("packages", {})
    if show_all:
        packages = all_packages
        click.echo(f"All Installed Packages ({len(packages)}):")
    else:
        key = {p.lower() for p in KEY_PACKAGES}
        packages = {k: v for k, v in all_packages.items() if k.lower() in key}
        click.echo(f"Key Packages ({len(packages)}/{len(all_packages)} total):")

    if not packages:
        click.echo("  (no package information available)")
        return

    click.echo("-" * 40)
    for name in sorted(packages, key=str.lower):
        click.echo(f"  {name}=={packages[name]}")
    if not show_all:
        click.echo("\nUse --all to show every package.")


def _show_local_env(json_flag: bool) -> None:
    import importlib.metadata

    data = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0 and r.stdout.strip():
            data["gpus"] = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
    except FileNotFoundError:
        pass

    installed = {}
    for pkg in ("torch", "ray", "nnsight", "transformers", "numpy"):
        try:
            installed[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            pass
    data["key_packages"] = installed

    if json_flag:
        click.echo(json.dumps(data, indent=2))
        return

    click.echo("Local System Environment")
    click.echo("=" * 60)
    click.echo()
    click.echo(f"Python: {data['python_version'].split()[0]}")
    click.echo(f"Platform: {data['platform']}")
    click.echo(f"Machine: {data['machine']}")
    click.echo()
    if data.get("gpus"):
        click.echo("GPUs:")
        for gpu in data["gpus"]:
            click.echo(f"  {gpu}")
        click.echo()
    if installed:
        click.echo("Key Packages:")
        for name, version in installed.items():
            click.echo(f"  {name}=={version}")
