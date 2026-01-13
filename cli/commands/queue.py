import json
import os
import pickle
import time
from datetime import timedelta
import click
import redis.asyncio as redis
import asyncio

from .util import check_prerequisites


@click.command()
@click.option('--json-output', 'json_flag', is_flag=True, help='Output raw JSON')
@click.option('--watch', is_flag=True, help='Watch mode (refresh every 2s)')
@click.option('--redis-url', default='redis://localhost:6379/', help='Redis URL (default: redis://localhost:6379/)')
def queue(json_flag: bool, watch: bool, redis_url: str):
    """View queue and processor status.

    Shows current queue state including active processors,
    queued requests, and executing requests.

    Examples:
        ndif queue                    # Quick overview
        ndif queue --json-output      # Raw JSON output
        ndif queue --watch            # Real-time monitoring
    """
    try:
        # Check prerequisites silently
        check_prerequisites(redis_url=redis_url)

        if watch:
            # Watch mode - loop forever
            try:
                while True:
                    # Fetch data BEFORE clearing screen to reduce flicker
                    data = asyncio.run(_fetch_queue_state(redis_url))

                    # Now clear and display atomically
                    click.clear()
                    _render_queue_state(data, json_flag)
                    click.echo("\n(Press Ctrl+C to exit watch mode)")
                    time.sleep(2)
            except KeyboardInterrupt:
                click.echo("\nExiting watch mode...")
                return
        else:
            # Single shot
            data = asyncio.run(_fetch_queue_state(redis_url))
            _render_queue_state(data, json_flag)

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


async def _fetch_queue_state(redis_url: str) -> dict:
    """Fetch queue state from the dispatcher via Redis streams."""
    redis_client = redis.Redis.from_url(redis_url)

    try:
        # Use PID and timestamp as unique response key
        response_key = f"queue_state_response:{os.getpid()}:{int(time.time() * 1000)}"

        # Send QUEUE_STATE_REQUEST event to dispatcher events stream
        await redis_client.xadd(
            "dispatcher:events",
            {
                "event_type": "queue_state_request",
                "response_key": response_key,
                "timestamp": str(time.time()),
            }
        )

        # Wait for response on the response key
        result = await redis_client.brpop(response_key, timeout=5)

        if result is None:
            raise Exception("Timeout waiting for queue state response")

        # Unpickle the response
        return pickle.loads(result[1])

    finally:
        await redis_client.aclose()


def _render_queue_state(data: dict, json_flag: bool):
    """Render queue state data with appropriate formatting."""
    if json_flag:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        format_queue_state_simple(data)


def format_queue_state_simple(queue_data: dict):
    """Pretty-print queue state overview."""

    click.echo("NDIF Queue Status")
    click.echo("=" * 60)
    click.echo()

    # Compute aggregates from processor data
    processors = queue_data.get('processors', {})
    num_processors = len(processors)
    total_queue_depth = sum(len(p.get('request_ids', [])) for p in processors.values())
    executing_requests = sum(1 for p in processors.values() if p.get('current_request_id') is not None)

    click.echo("Queue Overview:")
    click.echo(f"  Active Processors: {num_processors}")
    click.echo(f"  Total Queued Requests: {total_queue_depth}")
    click.echo(f"  Currently Executing: {executing_requests}")
    click.echo()

    if not processors:
        click.echo("No active processors.")
        return

    click.echo("Processors:")
    click.echo()

    for model_key, processor in processors.items():
        _print_processor(processor)
        click.echo()

    click.echo("-" * 60)
    click.echo("Use 'ndif queue --json-output' for raw data")


def _print_processor(processor: dict):
    """Print a single processor's state."""
    status = processor.get('status', 'unknown')
    current_request = processor.get('current_request_id')
    dedicated = processor.get('dedicated')
    request_ids = processor.get('request_ids', [])
    status_changed_at = processor.get('status_changed_at')
    current_request_started_at = processor.get('current_request_started_at')

    # Extract repo_id from model_key for display
    repo_id = _extract_repo_id_from_model_key(processor.get('model_key', 'unknown'))

    # Status emoji
    status_emoji = {
        'ready': '✓',
        'busy': '⚙️',
        'provisioning': '🔧',
        'deploying': '📦',
        'uninitialized': '❓',
        'cancelled': '✗',
    }.get(status, '•')

    click.echo(f"  {status_emoji} {repo_id}")

    # Show status with duration
    status_display = status.upper()
    if status_changed_at:
        duration = str(timedelta(seconds=int(time.time() - status_changed_at)))
        status_display = f"{status_display} (for {duration})"
    click.echo(f"    Status: {status_display}")

    if dedicated is not None:
        click.echo(f"    Dedicated: {'Yes' if dedicated else 'No'}")

    click.echo(f"    Queue Depth: {len(request_ids)}")

    if current_request:
        exec_display = current_request
        if current_request_started_at:
            exec_duration = str(timedelta(seconds=int(time.time() - current_request_started_at)))
            exec_display = f"{exec_display} (executing for {exec_duration})"
        click.echo(f"    Currently Executing: {exec_display}")

    if request_ids:
        # Show first few request IDs
        if len(request_ids) <= 3:
            click.echo(f"    Queued Requests: {', '.join(request_ids)}")
        else:
            shown = ', '.join(request_ids[:3])
            click.echo(f"    Queued Requests: {shown}, ... (+{len(request_ids) - 3} more)")


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
