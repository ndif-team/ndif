"""Main CLI entry point for NDIF"""

import click
from cli.commands import start, stop, restart, deploy, evict, queue
from cli.commands.status import status
from cli.commands.logs import logs
from cli.commands.kill import kill
from cli.commands.info import info
from cli.commands.env import env
from cli.commands.export import export

@click.group()
@click.version_option(package_name="ndif")
def cli():
    """CLI for managing NDIF (National Deep Inference Fabric).

    \b
    Start and stop services, deploy models, and monitor the cluster.

    \b
    Quick start:
        ndif start          Start all services
        ndif deploy gpt2    Deploy a model
        ndif status         View cluster status
        ndif stop           Stop all services

    \b
    See 'ndif <command> --help' for command-specific options.
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
cli.add_command(logs)
cli.add_command(kill)
cli.add_command(info)
cli.add_command(env)
cli.add_command(export)

if __name__ == "__main__":
    cli()