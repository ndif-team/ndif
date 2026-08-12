"""``ndif`` command-line entry point.

A click group that registers one command per module under ``commands/``. Every
knob is env-driven; a CWD ``.env`` (and an explicit ``--env-file``) is layered
in before any command runs.
"""

import click

from . import config
from .commands.deploy import deploy
from .commands.doctor import doctor
from .commands.env import env
from .commands.evict import evict
from .commands.scale import scale
from .commands.export import export
from .commands.info import info
from .commands.kill import kill
from .commands.logs import logs
from .commands.queue import queue
from .commands.restart import restart
from .commands.start import start
from .commands.status import status
from .commands.stop import stop


@click.group()
@click.option("--env-file", type=click.Path(exists=True, dir_okay=False),
              help="Load a .env file (overrides an auto-discovered ./.env).")
@click.version_option(package_name="ndif", prog_name="ndif")
def cli(env_file):
    """Manage NDIF services.

    \b
    ndif start          Start all services (redis, minio, ray, api)
    ndif deploy gpt2    Deploy a model
    ndif status         View cluster and deployment status
    ndif stop           Stop all services
    """
    config.load_env_files(env_file)


for _command in (start, stop, restart, deploy, scale, evict, status, queue, kill,
                 export, env, logs, info, doctor):
    cli.add_command(_command)
