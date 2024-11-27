"""Define the main entry point for the package."""
import click

from .cli.install import install
from .server import start


@click.group()
def cli():
    """Define the main entry point for the command-line interface."""
    pass


def main():
    """Define the main entry point for the command-line interface."""
    cli.add_command(start)
    cli.add_command(install)
    cli()


if __name__ == '__main__':
    main()
