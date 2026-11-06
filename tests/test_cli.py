import subprocess
import sys

from housesitter import __version__
from housesitter.core import cli


def test_version_flag(capsys):
    try:
        cli.main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert capsys.readouterr().out.strip() == f"housesitter {__version__}"


def test_no_args_prints_help_and_succeeds(capsys):
    assert cli.main([]) == 0
    assert "usage: housesitter" in capsys.readouterr().out


def test_runs_as_module():
    out = subprocess.run(
        [sys.executable, "-m", "housesitter", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert out.strip() == f"housesitter {__version__}"
