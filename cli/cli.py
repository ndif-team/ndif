"""Main CLI entry point for NDIF"""

import click

from cli.commands import start, stop, restart, deploy, evict, queue
from cli.commands.status import status

@click.group()
@click.version_option(package_name="ndif")
def cli():
    """NDIF - National Deep Inference Fabric

    The NDIF server, which performs deep inference and serves nnsight requests remotely.
    """
    pass


# Register commands
cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(deploy)
cli.add_command(evict)
cli.add_command(status)
cli.add_command(queue)

if __name__ == "__main__":
    cli()