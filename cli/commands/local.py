"""CLI command for running NDIF in local mode."""

import logging

import click


@click.group()
def local():
    """Run NDIF locally (single model, no infrastructure dependencies)."""
    pass


@local.command()
@click.argument("checkpoint")
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", default=None, type=int, help="Port (fail if in use). Auto-finds from 8289 if omitted.")
@click.option("--dtype", default="bfloat16", help="Model dtype (e.g. bfloat16, float16, float32).")
def start(checkpoint: str, host: str, port: int | None, dtype: str):
    """Start a local NDIF server for CHECKPOINT.

    CHECKPOINT is a HuggingFace model identifier (e.g. openai-community/gpt2).

    \b
    Examples:
        ndif local start openai-community/gpt2
        ndif local start meta-llama/Llama-3.1-8B --port 8300
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    from src.local.server import run

    run(checkpoint=checkpoint, host=host, port=port, dtype=dtype)
