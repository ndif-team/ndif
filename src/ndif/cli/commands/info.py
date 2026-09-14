"""``ndif info`` — configuration, tracked services, and connectivity."""

import json

import click

from .. import config
from ..lib.checks import check_api, check_minio, check_ray, check_redis
from ..service import SERVICES
from ..state import State

# (label, env var, reachability check) for each backing service.
_ENDPOINTS = [
    ("redis", "NDIF_REDIS_URL", check_redis),
    ("minio", "NDIF_OBJECT_STORE_URL", check_minio),
    ("api", "NDIF_API_URL", check_api),
    ("ray", "NDIF_RAY_ADDRESS", check_ray),
]


@click.command()
@click.option("--json-output", "json_flag", is_flag=True, help="Output as JSON.")
def info(json_flag):
    """Show CLI configuration, tracked service PIDs, and connectivity."""
    state = State.from_env()
    services = {}
    for svc in SERVICES:
        pid = state.running_pid(svc.name)
        services[svc.name] = {"pid": pid, "running": pid is not None}
    endpoints = {
        name: {"url": config.get(var), "reachable": fn(config.get(var))}
        for name, var, fn in _ENDPOINTS
    }

    if json_flag:
        click.echo(json.dumps({
            "home": config.get("NDIF_HOME"),
            "services": services,
            "endpoints": endpoints,
        }, indent=2))
        return

    click.echo("NDIF")
    click.echo("=" * 40)
    click.echo(f"Home: {config.get('NDIF_HOME')}")
    click.echo()

    click.echo("Services:")
    for svc in SERVICES:
        s = services[svc.name]
        state_str = f"🟢 running (pid {s['pid']})" if s["running"] else "⚪ stopped"
        click.echo(f"  {svc.name}: {state_str}")
    click.echo()

    click.echo("Connectivity:")
    for name, _, _ in _ENDPOINTS:
        e = endpoints[name]
        click.echo(f"  {'✓' if e['reachable'] else '✗'} {name} {e['url']}")
