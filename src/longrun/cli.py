"""Command-line entry point for longrun."""

from typing import Annotated

import typer

from longrun import __version__

app = typer.Typer(
    name="longrun",
    help="Holistic, race-less endurance-training analytics.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the installed version and exit."""
    if value:
        typer.echo(f"longrun {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True),
    ] = False,
) -> None:
    """Holistic, race-less endurance-training analytics."""
