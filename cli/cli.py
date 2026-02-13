"""Main CLI entry point for NDIF"""

import click
from cli.commands import start, stop, restart, deploy, scale, evict, queue
from cli.commands.status import status
from cli.commands.logs import logs
from cli.commands.kill import kill
from cli.commands.info import info
from cli.commands.env import env

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
cli.add_command(scale)
cli.add_command(evict)
cli.add_command(status)
cli.add_command(queue)
cli.add_command(logs)
cli.add_command(kill)
cli.add_command(info)
cli.add_command(env)

if __name__ == "__main__":
    cli()
