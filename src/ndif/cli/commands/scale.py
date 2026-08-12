"""``ndif scale`` — add replicas matching the ones already running."""

import click

from ..lib.scale import scale as scale_lib


@click.command()
@click.argument("checkpoint")
@click.option("-n", "count", default=1, show_default=True,
              help="How many replicas to ADD (additive, like deploy --replicas).")
@click.option("--revision", default=None, help="Model revision/branch.")
@click.option("--actor-class", default=None,
              help="Override the actor class instead of matching the running one.")
@click.option("--dtype", default=None, help="Override the dtype instead of matching.")
@click.option("--gpus", type=int, default=None,
              help="Override the GPU count instead of matching.")
@click.option("--execution-timeout", "execution_timeout_seconds", type=float, default=None,
              help="Override the execution timeout instead of matching.")
@click.option("--trusted", is_flag=True,
              help="Run the model's own repo code (HF trust_remote_code).")
@click.option("--pinned", is_flag=True, help="Add as pinned (won't be evicted).")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
def scale(checkpoint, count, revision, actor_class, dtype, gpus,
          execution_timeout_seconds, trusted, pinned, ray_address):
    """Add replicas of a model, matching how it is already served.

    CHECKPOINT: the model to grow (e.g. "meta-llama/Llama-3.2-1B").

    Same as ``deploy --replicas`` except for where the unspecified settings come
    from: ``deploy`` uses the controller's defaults, ``scale`` copies a replica
    already running. So growing a tensor-parallel model gives you more
    tensor-parallel replicas rather than a differently-served one under the same
    model key.

    Any option given here is used as-is, and also decides which live replica
    counts as a match to copy the rest from. With nothing running there is
    nothing to copy and this behaves as a plain deploy.

    \b
    Examples:
        ndif scale meta-llama/Llama-3.2-1B
        ndif scale meta-llama/Llama-3.2-1B -n 2
        ndif scale gpt2 -n 1 --dtype float32
    """
    result = scale_lib(
        checkpoint,
        n=count,
        revision=revision,
        actor_class=actor_class,
        dtype=dtype,
        gpus=gpus,
        execution_timeout_seconds=execution_timeout_seconds,
        trusted=trusted,
        pinned=pinned,
        ray_address=ray_address,
        on_message=click.echo,
    )
    if result["error"]:
        raise click.ClickException(result["error"])
