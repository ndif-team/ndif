"""Info command for NDIF - view session and configuration details."""

import json
import click

from .session import (
    get_current_session,
    get_session_root,
    get_env,
    ENV_VARS,
    is_port_in_use,
)
from .checks import check_redis, check_minio, check_api, check_ray


@click.command()
@click.option('--json-output', 'json_flag', is_flag=True, help='Output as JSON')
@click.option('--env', 'show_env', is_flag=True, help='Show all NDIF environment variables')
def info(json_flag: bool, show_env: bool):
    """Show current session and configuration information.

    Displays:
    - Active session details
    - Service status (running/stopped)
    - Configuration values
    - Environment variable settings

    Examples:
        ndif info                  # Show session and status
        ndif info --env            # Include all environment variables
        ndif info --json-output    # Output as JSON
    """
    session = get_current_session()

    if json_flag:
        _output_json(session, show_env)
    else:
        _output_human(session, show_env)


def _output_json(session, show_env: bool):
    """Output session info as JSON."""
    data = {
        "session": None,
        "services": {},
        "environment": {} if show_env else None,
    }

    if session:
        data["session"] = {
            "id": session.config.session_id,
            "path": str(session.path),
            "created_at": session.config.created_at,
        }
        data["services"] = {
            name: {
                "configured_port": svc.port,
                "managed": svc.managed,
                "marked_running": svc.running,
                "actually_running": is_port_in_use(svc.port),
            }
            for name, svc in session.config.services.items()
        }

    if show_env:
        data["environment"] = {
            name: get_env(name)
            for name in ENV_VARS.keys()
        }

    click.echo(json.dumps(data, indent=2))


def _output_human(session, show_env: bool):
    """Output session info in human-readable format."""

    click.echo("NDIF Session Information")
    click.echo("=" * 60)
    click.echo()

    # Session info
    if session:
        click.echo(f"Active Session: {session.config.session_id}")
        click.echo(f"  Path: {session.path}")
        click.echo(f"  Created: {session.config.created_at}")
        click.echo()

        # Service status
        click.echo("Services:")
        for name, svc in session.config.services.items():
            port_in_use = is_port_in_use(svc.port)

            if svc.running and port_in_use:
                status = "🟢 running"
            elif svc.running and not port_in_use:
                status = "🟡 marked running but port not in use"
            elif not svc.running and port_in_use:
                status = "🟡 stopped but port in use (external?)"
            else:
                status = "⚪ stopped"

            managed_str = "managed" if svc.managed else "external"
            click.echo(f"  {name}: {status} (port {svc.port}, {managed_str})")

        click.echo()

        # Configuration summary
        click.echo("Configuration:")
        click.echo(f"  Broker URL: {session.config.broker_url}")
        click.echo(f"  Object Store URL: {session.config.object_store_url}")
        click.echo(f"  API URL: {session.config.api_url}")
        click.echo(f"  Ray Address: {session.config.ray_address}")
        click.echo(f"  Ray Dashboard: {session.config.ray_dashboard_host}:{session.config.ray_dashboard_port}")

    else:
        click.echo("No active session")
        click.echo()
        click.echo("Start a session with: ndif start")
        click.echo()

        # Show defaults that would be used
        click.echo("Default Configuration (from environment):")
        click.echo(f"  Session Root: {get_env('NDIF_SESSION_ROOT')}")
        click.echo(f"  Broker URL: {get_env('NDIF_BROKER_URL')}")
        click.echo(f"  Object Store URL: {get_env('NDIF_OBJECT_STORE_URL')}")
        click.echo(f"  API URL: {get_env('NDIF_API_URL')}")
        click.echo(f"  Ray Address: {get_env('NDIF_RAY_ADDRESS')}")
        click.echo(f"  Ray Temp Dir: {get_env('NDIF_RAY_TEMP_DIR')}")

    # Environment variables
    if show_env:
        click.echo()
        click.echo("Environment Variables:")
        click.echo("-" * 60)

        for name in sorted(ENV_VARS.keys()):
            value = get_env(name)
            default = ENV_VARS[name]
            is_default = value == default

            if is_default:
                click.echo(f"  {name}={value} (default)")
            else:
                click.echo(f"  {name}={value} (custom, default: {default})")

    click.echo()
    click.echo("-" * 60)

    # Quick connectivity check
    click.echo("Quick Connectivity Check:")

    broker_url = session.config.broker_url if session else get_env("NDIF_BROKER_URL")
    object_store_url = session.config.object_store_url if session else get_env("NDIF_OBJECT_STORE_URL")
    api_url = session.config.api_url if session else get_env("NDIF_API_URL")
    ray_address = session.config.ray_address if session else get_env("NDIF_RAY_ADDRESS")

    if check_redis(broker_url):
        click.echo(f"  ✓ Broker reachable at {broker_url}")
    else:
        click.echo(f"  ✗ Broker not reachable at {broker_url}")

    if check_minio(object_store_url):
        click.echo(f"  ✓ Object store reachable at {object_store_url}")
    else:
        click.echo(f"  ✗ Object store not reachable at {object_store_url}")

    if check_api(api_url):
        click.echo(f"  ✓ API reachable at {api_url}")
    else:
        click.echo(f"  ✗ API not reachable at {api_url}")

    if check_ray(ray_address):
        click.echo(f"  ✓ Ray reachable at {ray_address}")
    else:
        click.echo(f"  ✗ Ray not reachable at {ray_address}")
