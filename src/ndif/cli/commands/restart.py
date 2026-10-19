"""``ndif restart`` — restart replicas of a model deployment."""

import click

from ..lib.restart import NDIFConnectivityError, restart as restart_lib


@click.command()
@click.argument("checkpoint")
@click.option("--revision", default=None, help="Model revision/branch (default: unset).")
@click.option("--task", default=None,
              help="Pipeline task of the deployment to restart (inferred when unset).")
@click.option("--trust-remote-code", "trust_remote_code", is_flag=True,
              help="The deployment was made with its own modelling code allowed (HF "
                   "trust_remote_code); part of the key, so needed to name it.")
@click.option("--replica", default=None,
              help="Target a single replica by id (default: restart all replicas).")
@click.option("--ray-address", default=None, help="Ray address (default: NDIF_RAY_ADDRESS).")
def restart(checkpoint, revision, task, trust_remote_code, replica, ray_address):
    """Restart replicas of a model deployment.

    CHECKPOINT: model checkpoint (e.g. "gpt2", "meta-llama/Llama-3.1-8B").

    Restarts every HOT replica by default; use ``--replica`` to target one.
    Useful for clearing cached state, reloading weights, or recovering errors.

    \b
    Examples:
        ndif restart gpt2
        ndif restart gpt2 --replica abc12
        ndif restart meta-llama/Llama-3.1-8B --revision main
    """
    try:
        restart_lib(checkpoint, revision=revision, task=task,
                    trust_remote_code=trust_remote_code, replica=replica,
                    ray_address=ray_address, on_message=click.echo)
    except NDIFConnectivityError as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
    except Exception as e:
        click.echo(f"✗ Error: {e}", err=True)
        raise click.Abort()
