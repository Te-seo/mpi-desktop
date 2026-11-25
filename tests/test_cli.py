from __future__ import annotations

from click.testing import CliRunner

from mpi.cli import cli


def test_cli_help_renders_without_crash() -> None:
    """Pin help output. Also guards against re-introducing non-ASCII chars
    that would crash on cp949 (Korean Windows) consoles when click writes
    the docstring to stdout."""
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "MPI" in result.output
    assert "devices" in result.output
    assert "packages" in result.output
    assert "check" in result.output
    # Non-ASCII chars in the click group docstring would not necessarily
    # crash CliRunner (uses utf-8 buffer), but pin the ASCII contract here.
    assert all(ord(c) < 128 for c in result.output.splitlines()[1])
