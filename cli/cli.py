"""Main CLI entry point for NDIF"""

import click

from cli.commands import start, stop


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


if __name__ == "__main__":
    cli()