"""The `housesitter` command line.

Only `--version` exists so far. The remaining commands in
docs/ARCHITECTURE.md §3 are planned.
"""

from __future__ import annotations

import argparse
import sys

from housesitter import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="housesitter",
        description="Read-only data gathering for Muse. See docs/ARCHITECTURE.md.",
    )
    parser.add_argument("--version", action="version", version=f"housesitter {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help(sys.stdout)
    return 0
