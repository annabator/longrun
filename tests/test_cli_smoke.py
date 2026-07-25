"""Smoke tests for the CLI entry point."""

from typer.testing import CliRunner

from longrun import __version__
from longrun.cli import app

runner = CliRunner()


def test_version_flag_reports_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_bare_invocation_shows_help() -> None:
    """Running `longrun` with no arguments should describe the tool, not error out."""
    result = runner.invoke(app, [])

    assert "endurance-training analytics" in result.stdout
