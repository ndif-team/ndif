"""``ndif queue`` — view queue and processor status."""

import json
import time
from datetime import timedelta

import click

from ..lib.events import fetch_queue_state
from ..lib.models import extract_repo_id_from_model_key


@click.command()
@click.option("--json-output", "json_flag", is_flag=True, help="Output raw JSON.")
@click.option("--watch", is_flag=True, help="Watch mode (refresh every 2s).")
@click.option("--redis-url", default=None, help="Redis URL (default: NDIF_REDIS_URL).")
def queue(json_flag, watch, redis_url):
    """View queue and processor status.

    Shows each active per-model processor: its status, replica pool, queue
    depth, and which requests are queued or executing.

    \b
    Examples:
        ndif queue
        ndif queue --json-output
        ndif queue --watch
    """
    try:
        if watch:
            try:
                while True:
                    data = fetch_queue_state(redis_url)
                    click.clear()
                    _render(data, json_flag)
                    click.echo("\n(Press Ctrl+C to exit watch mode)")
                    time.sleep(2)
            except KeyboardInterrupt:
                click.echo("\nExiting watch mode...")
                return
        else:
            _render(fetch_queue_state(redis_url), json_flag)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


def _render(data: dict, json_flag: bool) -> None:
    if json_flag:
        click.echo(json.dumps(data, indent=2, default=str))
        return

    processors = data.get("processors", {})
    total_queued = sum(p.get("queue_size", 0) for p in processors.values())
    executing = sum(
        1 for p in processors.values() for r in p.get("replicas", []) if r.get("busy")
    )

    click.echo("NDIF Queue Status")
    click.echo("=" * 60)
    click.echo()
    click.echo("Overview:")
    click.echo(f"  Active processors: {len(processors)}")
    click.echo(f"  Queued requests: {total_queued}")
    click.echo(f"  Executing: {executing}")
    click.echo()

    if not processors:
        click.echo("No active processors.")
        return

    for processor in processors.values():
        _print_processor(processor)
        click.echo()

    click.echo("-" * 60)
    click.echo("Use 'ndif queue --json-output' for raw data")


def _print_processor(p: dict) -> None:
    repo_id = extract_repo_id_from_model_key(p.get("model_key", "unknown"))
    click.echo(f"  {repo_id}")

    status_line = p.get("status", "unknown").upper()
    changed = p.get("status_changed_at")
    if changed:
        status_line += f" (for {timedelta(seconds=int(time.time() - changed))})"
    click.echo(f"    Status: {status_line}")

    replicas = p.get("replicas", [])
    click.echo(f"    Replicas: {len(replicas)}")
    click.echo(f"    Queue depth: {p.get('queue_size', 0)}")

    for r in replicas:
        if r.get("busy") and r.get("current_request_id"):
            dur = ""
            if r.get("current_started_at"):
                dur = f" (for {timedelta(seconds=int(time.time() - r['current_started_at']))})"
            click.echo(f"    ⚙ [{r['replica_id']}] executing {r['current_request_id']}{dur}")

    request_ids = p.get("request_ids", [])
    if request_ids:
        shown = ", ".join(request_ids[:3])
        more = f", ... (+{len(request_ids) - 3} more)" if len(request_ids) > 3 else ""
        click.echo(f"    Queued: {shown}{more}")
