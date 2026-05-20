"""Main CLI entry point for NDIF"""

import click
from dotenv import load_dotenv

from .commands import start, stop, restart, deploy, evict, queue
from .commands.status import status
from .commands.logs import logs
from .commands.kill import kill
from .commands.info import info
from .commands.env import env
from .commands.export import export
from .commands.doctor import doctor

@click.group()
@click.option('--env-file', type=click.Path(exists=True, dir_okay=False),
              help='Path to a .env file to load (overrides any auto-discovered .env).')
@click.version_option(package_name="ndif")
def cli(env_file):
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

    \b
    .env files (auto-discovered, later sources override earlier):
        <repo>/.env.example   committed defaults (editable installs only)
        <repo>/.env           your repo-local overrides (editable installs only)
        ./.env                CWD-relative .env (works in any install)
        --env-file PATH       explicit override applied last
    """
    if env_file:
        load_dotenv(env_file, override=True)


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
cli.add_command(doctor)

if __name__ == "__main__":
    cli()