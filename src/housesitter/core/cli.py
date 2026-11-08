"""The `housesitter` command line.

Read-only data gathering for Muse: `invoke` runs one command through
the runner pipeline, `list`/`describe`/`doctor` inspect the registry,
`dropins`/`install-commands` integrate with the SDK, and
`config-check` validates the config file. Never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

from housesitter import __version__
from housesitter.core import builtins as _builtins
from housesitter.core import dropins as _dropins
from housesitter.core import envelope as _envelope
from housesitter.core import runner as _runner
from housesitter.core.config import ConfigError, load_config
from housesitter.core.registry import RegistryError, load_registry

DEFAULT_COMMANDS_DIR = _builtins.DEFAULT_COMMANDS_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="housesitter",
        description="Read-only data gathering for Muse. See docs/ARCHITECTURE.md.",
    )
    parser.add_argument("--version", action="version", version=f"housesitter {__version__}")
    sub = parser.add_subparsers(dest="subcommand")

    invoke = sub.add_parser("invoke", help="Run one command and print its envelope.")
    invoke.add_argument("command", help="Full command name, e.g. housesitter.doctor.")
    invoke.add_argument(
        "--params",
        default=None,
        help="Params as a JSON object (wins over stdin).",
    )

    listing = sub.add_parser("list", help="List available commands.")
    listing.add_argument(
        "--json", dest="as_json", action="store_true", help="Print a JSON list."
    )

    sub.add_parser("describe", help="Same as: invoke housesitter.describe.")

    doctor = sub.add_parser("doctor", help="Same as: invoke housesitter.doctor.")
    doctor.add_argument(
        "--check",
        action="store_true",
        help="Also run each enabled module's read-only connectivity check.",
    )

    dropins = sub.add_parser("dropins", help="Write SDK drop-in files.")
    dropins.add_argument("--out", default=None, help="Directory to write files into.")
    dropins.add_argument("--exec", dest="exec_path", default=None)
    dropins.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Write one JSON object {filename: content} to stdout instead.",
    )

    install = sub.add_parser(
        "install-commands", help="Install drop-in files with sudo install."
    )
    install.add_argument("--exec", dest="exec_path", default=None)
    install.add_argument("--commands-dir", default=None)
    install.add_argument("--dry-run", action="store_true")

    sub.add_parser("config-check", help="Validate the config file and exit.")

    # No flags: bind, port, and hosts come from housesitter.dashboard in
    # config. Not a Muse command: no drop-in is generated for it.
    sub.add_parser("serve", help="Serve the read-only dashboard.")
    return parser


def _error_line(code: str, reason: str) -> str:
    return _envelope.format_error("housesitter", code, reason)


def _first_sentence(description: str) -> str:
    # A sentence ends at a period followed by a space (or the end), so
    # names like ``SKILL.md`` stay whole.
    flat = " ".join(description.split())
    return re.split(r"\.(?:\s|$)", flat, maxsplit=1)[0].strip()


def _resolve_exec(explicit: str | None) -> str:
    if explicit is not None:
        return _dropins.check_exec_path(explicit)
    return _builtins.default_exec_path()


def cmd_list(registry: object, config: object, *, as_json: bool, out: object) -> int:
    rows = []
    for full_name in sorted(registry.commands):  # type: ignore[attr-defined]
        registered = registry.commands[full_name]  # type: ignore[attr-defined]
        try:
            enabled = bool(config.module(registered.module_id).enabled)  # type: ignore[attr-defined]
        except Exception:
            enabled = False
        rows.append(
            (
                full_name,
                "enabled" if enabled else "disabled",
                _first_sentence(registered.command.description),
            )
        )
    for short in sorted(_builtins.CORE_COMMANDS):
        full_name = f"housesitter.{short}"
        rows.append(
            (full_name, "core", _first_sentence(_builtins.CORE_COMMANDS[short].description))
        )
    rows.sort(key=lambda row: row[0])
    if as_json:
        out.write(  # type: ignore[attr-defined]
            json.dumps(
                [
                    {"name": name, "status": status, "description": text}
                    for name, status, text in rows
                ],
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            + "\n"
        )
    else:
        for name, status, text in rows:
            out.write(f"{name}\t{status}\t{text}\n")  # type: ignore[attr-defined]
    return 0


def cmd_config_check(registry: object, config: object, home: str, out: object) -> int:
    out.write(  # type: ignore[attr-defined]
        f"config ok: {_builtins.display_path(str(config.path), home)}\n"  # type: ignore[attr-defined]
    )
    for entry in registry.modules:  # type: ignore[attr-defined]
        try:
            enabled = bool(config.module(entry.id).enabled)  # type: ignore[attr-defined]
        except Exception:
            enabled = False
        out.write(f"{entry.id}: {'enabled' if enabled else 'disabled'}\n")  # type: ignore[attr-defined]
    return 0


def cmd_dropins(
    registry: object, args: argparse.Namespace, out: object
) -> tuple[int, str | None]:
    """Returns ``(exit_code, error_line_or_None)``; prints files or JSON."""
    try:
        exec_path = _resolve_exec(args.exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    try:
        files = _dropins.generate(registry, exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    if args.print_only:
        out.write(  # type: ignore[attr-defined]
            json.dumps(files, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        )
        return 0, None
    if not args.out:
        return 1, _error_line("config_invalid", "--out is required without --print")
    try:
        created = not os.path.exists(args.out)
        os.makedirs(args.out, mode=0o755, exist_ok=True)
        if created:
            os.chmod(args.out, 0o755)
        names = _dropins.write_files(files, args.out)
    except OSError:
        return 1, _error_line(
            "config_invalid", f"cannot write drop-in files in {args.out}"
        )
    for name in names:
        out.write(name + "\n")  # type: ignore[attr-defined]
    return 0, None


def cmd_install_commands(
    registry: object,
    args: argparse.Namespace,
    out: object,
    *,
    runner: object = None,
    commands_dir: object = None,
) -> tuple[int, str | None]:
    """Returns ``(exit_code, error_line_or_None)``; prints every step."""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        return (
            1,
            _error_line(
                "config_invalid",
                "refusing to run install-commands as root; "
                "run as the service account",
            ),
        )
    try:
        exec_path = _resolve_exec(args.exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    try:
        files = _dropins.generate(registry, exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    dest = (
        args.commands_dir
        or (str(commands_dir) if commands_dir is not None else None)
        or DEFAULT_COMMANDS_DIR
    )
    tmpdir = tempfile.mkdtemp(prefix="housesitter-dropins-")
    try:
        names = _dropins.write_files(files, tmpdir)
        try:
            present = set(os.listdir(dest)) if os.path.isdir(dest) else set()
        except OSError:
            present = set()
        stale = sorted(
            name
            for name in present
            if name.startswith("housesitter.") and name.endswith(".json")
            and name not in files
        )
        run = runner if runner is not None else subprocess.run
        if not args.dry_run:
            for name in names:
                argv = [
                    "sudo", "install", "-o", "root", "-g", "root", "-m", "644",
                    os.path.join(tmpdir, name), os.path.join(dest, name),
                ]
                try:
                    run(argv, check=True)  # type: ignore[operator]
                except Exception:
                    return 1, _error_line(
                        "config_invalid", f"could not install {name}"
                    )
        else:
            for name in names:
                out.write(f"would install {name} -> {dest}/{name}\n")  # type: ignore[attr-defined]
        for name in stale:
            out.write(  # type: ignore[attr-defined]
                f"warning: stale drop-in file {name} is not generated; "
                "left in place\n"
            )
        out.write("restart with: sudo systemctl restart musegadget\n")  # type: ignore[attr-defined]
        out.write(  # type: ignore[attr-defined]
            "warning: restarting musegadget stops any command "
            "Muse is running right now\n"
        )
        return 0, None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main(
    argv: list[str] | None = None,
    *,
    env: dict | None = None,
    stdin: object = None,
    stdout: object = None,
    stderr: object = None,
    _exit: object = None,
    registry: object = None,
    config: object = None,
    commands_dir: object = None,
    install_runner: object = None,
) -> int:
    if _exit is None:
        _exit = os._exit
    env_map = os.environ if env is None else env
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    stream_in = stdin if stdin is not None else sys.stdin.buffer
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.subcommand is None:
        parser.print_help(out)
        return 0

    if registry is None:
        try:
            registry = load_registry()
        except RegistryError as exc:
            err.write(_error_line("config_invalid", str(exc)) + "\n")
            return 1

    if args.subcommand in ("invoke", "describe", "doctor"):
        stdin_outcome = None
        if args.subcommand == "invoke":
            full_name = args.command
            if args.params is not None:
                raw: bytes | str | dict | None = args.params
            else:
                try:
                    raw = stream_in.read(_runner.MAX_PARAMS_BYTES + 1)  # type: ignore[attr-defined]
                except OSError:
                    raw = None
                    try:
                        err_module = registry.get_command(full_name).module_id  # type: ignore[attr-defined]
                    except (AttributeError, KeyError):
                        err_module = "housesitter"
                    stdin_outcome = _runner.Outcome(
                        exit_code=1,
                        envelope=_envelope.build_error(
                            "bad_params", "cannot read params from stdin"
                        ),
                        error_module=err_module,
                        error_code="bad_params",
                        error_reason="cannot read params from stdin",
                    )
        elif args.subcommand == "describe":
            full_name = "housesitter.describe"
            raw = None
        else:
            full_name = "housesitter.doctor"
            raw = {"check": True} if args.check else None
        if stdin_outcome is not None:
            outcome = stdin_outcome
        else:
            outcome = _runner.run_command(
                full_name,
                raw,
                registry=registry,
                env=env_map,
                config=config,
                commands_dir=commands_dir,  # type: ignore[arg-type]
                log_stream=err,
            )
        out.write(_envelope.serialize(outcome.envelope) + "\n")  # type: ignore[attr-defined]
        try:
            out.flush()  # type: ignore[attr-defined]
        except Exception:
            pass
        if outcome.exit_code == 0:
            return 0
        err.write(_runner.format_error_line(outcome) + "\n")
        try:
            err.flush()
        except Exception:
            pass
        if outcome.worker_alive:
            _exit(1)  # type: ignore[operator]
        return 1

    if args.subcommand == "list":
        try:
            cfg = config if config is not None else load_config(registry, env=env_map)
        except ConfigError as exc:
            err.write(_error_line("config_invalid", f"{exc.key}: {exc.problem}") + "\n")
            return 1
        return cmd_list(registry, cfg, as_json=args.as_json, out=out)

    if args.subcommand == "config-check":
        try:
            cfg = config if config is not None else load_config(registry, env=env_map)
        except ConfigError as exc:
            err.write(_error_line("config_invalid", f"{exc.key}: {exc.problem}") + "\n")
            return 1
        return cmd_config_check(registry, cfg, _builtins.home_of(env_map), out)

    if args.subcommand == "dropins":
        code, line = cmd_dropins(registry, args, out)
        if line is not None:
            err.write(line + "\n")
        return code

    if args.subcommand == "install-commands":
        code, line = cmd_install_commands(
            registry, args, out, runner=install_runner,
            commands_dir=commands_dir,
        )
        if line is not None:
            err.write(line + "\n")
        return code

    if args.subcommand == "serve":
        from housesitter.core import serve as _serve

        return _serve.serve_command(registry, env_map, out, err)

    parser.error(f"unknown subcommand {args.subcommand}")
    return 2
