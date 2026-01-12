"""Kill command for NDIF - cancel a specific request"""

import os
import pickle
import time
import click
import redis.asyncio as redis
import asyncio


@click.command()
@click.argument('request_id')
@click.option('--redis-url', default='redis://localhost:6379/', help='Redis URL (default: redis://localhost:6379/)')
def kill(request_id: str, redis_url: str):
    """Cancel a specific request by ID.

    REQUEST_ID: The ID of the request to cancel

    This command will:
    - Remove the request from the queue if it's waiting
    - Cancel the request if it's currently executing

    Examples:
        ndif kill abc123                     # Cancel request abc123
        ndif kill abc123 --redis-url redis://... # Use custom Redis URL
    """
    try:
        result = asyncio.run(_kill_request(redis_url, request_id))

        # Display result based on status
        status = result.get("status")
        message = result.get("message", "")

        if status == "removed_from_queue":
            click.echo(f"✓ {message}")

        elif status == "cancelled_execution":
            click.echo(f"✓ {message}")

        elif status == "not_found":
            click.echo(f"✗ Request {request_id} not found", err=True)
            click.echo(f"   Hint: Use 'ndif queue' to see active requests", err=True)
            raise click.Abort()

        elif status == "error":
            click.echo(f"✗ Error: {message}", err=True)
            raise click.Abort()

        else:
            click.echo(f"✗ Unknown status: {status}", err=True)
            raise click.Abort()

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.Abort()


async def _kill_request(redis_url: str, request_id: str) -> dict:
    """Send kill request to dispatcher and wait for response."""
    redis_client = redis.Redis.from_url(redis_url)

    try:
        # Use PID and timestamp as unique response key
        response_key = f"kill_response:{os.getpid()}:{int(time.time() * 1000)}"

        # Send KILL_REQUEST event to dispatcher events stream
        await redis_client.xadd(
            "dispatcher:events",
            {
                "event_type": "kill_request",
                "request_id": request_id,
                "response_key": response_key,
                "timestamp": str(time.time()),
            }
        )

        # Wait for response on the response key
        result = await redis_client.brpop(response_key, timeout=5)

        if result is None:
            raise Exception("Timeout waiting for kill response")

        # Unpickle the response
        return pickle.loads(result[1])

    finally:
        await redis_client.aclose()
