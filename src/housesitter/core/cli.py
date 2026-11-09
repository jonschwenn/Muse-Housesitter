"""The `housesitter` command line.

Read-only data gathering for Muse: `invoke` runs one command through
the runner pipeline, `list`/`describe`/`doctor` inspect the registry,
`dropins`/`install-commands` integrate with the SDK, and
`config-check` validates the config file. Never prints secrets.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading

from housesitter import __version__
from housesitter.core import builtins as _builtins
from housesitter.core import dropins as _dropins
from housesitter.core import envelope as _envelope
from housesitter.core import runner as _runner
from housesitter.core import stdio as _stdio
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


# Fixed seal-mode diagnostics for an unexpected failure: no exception
# text, which may be untrusted or secret — always the same bytes.
_UNEXPECTED_CODE = "upstream_unexpected"
_UNEXPECTED_REASON = "the command failed unexpectedly"

# Bound for final delivery in seal mode: a daemon watchdog hard-exits 1
# if a stalled reader keeps the real streams blocked past this. Started
# when final delivery begins (immediately before the first CLI byte is
# written to the real stdout or stderr on each path, after all
# subprocess, file, network, and handler work, and on the _seal_fail
# path), never while a handler or any other slow legitimate work is
# still running: commands may legitimately run for minutes.
_SEAL_WATCHDOG_S = 5


def _default_sigint_for_seal() -> None:
    """Seal mode: a SIGINT kills the process immediately, like SIGTERM.

    The seal path ends with ``os._exit`` and must never fall back into
    normal interpreter shutdown (module atexit hooks, non-daemon
    threads, finalizers on module-reachable streams). Restoring the
    default disposition means a SIGINT arriving anywhere in the final
    delivery/seal/hard-exit transition terminates the process at once
    with a nonzero status instead of raising KeyboardInterrupt into
    that shutdown machinery. Main thread only: ``signal.signal`` raises
    elsewhere, and any failure just keeps the previous disposition.
    Restore mode leaves signal handling unchanged.
    """
    try:
        if threading.current_thread() is not threading.main_thread():
            return
    except Exception:
        return
    try:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
    except (OSError, RuntimeError, ValueError):
        pass


def _arm_seal_watchdog(exit_fn: object, seal_note: dict | None) -> None:
    """Start the final-delivery watchdog; idempotent per note.

    A daemon timer that hard-exits 1 after ``_SEAL_WATCHDOG_S`` if the
    process is still alive: a reader that stalls the real stdout or
    stderr (full pipe) must fail promptly, never hang past ``os._exit``.
    In production ``exit_fn`` is ``os._exit``. Only the first call per
    ``seal_note`` arms; without a note every call arms (harmless: each
    timer exits 1 all the same).
    """
    if seal_note is not None:
        if seal_note.get("watchdog_armed"):
            return
        seal_note["watchdog_armed"] = True
    try:
        timer = threading.Timer(_SEAL_WATCHDOG_S, exit_fn, args=(1,))  # type: ignore[arg-type]
        timer.daemon = True
        timer.start()
    except Exception:
        pass


def _delivery_started(
    guard: object,
    seal_mode: bool,
    exit_fn: object,
    seal_note: dict | None,
    *,
    to_stdout: bool = True,
) -> None:
    """Mark seal-mode output started; arm the delivery watchdog once.

    Call immediately before the first CLI write of the envelope,
    output, or error lines on every subcommand path — after all
    subprocess, file, network, and handler work, never before slow
    legitimate work — so the watchdog cannot cut a command off
    mid-flight (repeat calls are free: only the first marks and arms).
    Only a write (or attempted write) to the real stdout marks stdout
    output started: a stderr-only failure must not suppress the fixed
    stdout envelope while stdout could still carry it. Once stdout
    output started, a later seal-mode failure must not write a
    replacement envelope — only the fixed stderr line and exit 1 — so
    the stdout contract (zero or one JSON object) holds even when a
    flush failed or an exception lands mid-write.
    """
    if seal_note is not None:
        seal_note["output_started"] = True
        if to_stdout:
            seal_note["stdout_started"] = True
    if seal_mode and guard is not None:
        _arm_seal_watchdog(exit_fn, seal_note)


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
    registry: object, args: argparse.Namespace, out: object,
    *,
    on_first_write: object = None,
) -> tuple[int, str | None]:
    """Returns ``(exit_code, error_line_or_None)``; prints files or JSON.

    ``on_first_write`` runs immediately before the first ``out`` write
    (never on error paths, which write nothing here): the caller arms
    final-delivery protection only after all file work is done.
    """
    def _note() -> None:
        if on_first_write is not None:
            on_first_write()  # type: ignore[operator]

    try:
        exec_path = _resolve_exec(args.exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    try:
        files = _dropins.generate(registry, exec_path)
    except _dropins.DropinError as exc:
        return 1, _error_line("config_invalid", str(exc))
    if args.print_only:
        _note()
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
    _note()
    for name in names:
        out.write(name + "\n")  # type: ignore[attr-defined]
    return 0, None


def _run_child(run: object, argv: list) -> object:
    """Run one ``sudo install`` with output to the real streams.

    Under the stdio guard, plain inheritance would send the child's
    output into the discarding sink, so the reserved real fds are passed
    explicitly. Runners must accept ``stdout=``/``stderr=`` keywords.
    """
    fds = _stdio.current_real_fds()
    if fds is not None:
        return run(argv, check=True, stdout=fds[0], stderr=fds[1])  # type: ignore[operator]
    return run(argv, check=True)  # type: ignore[operator]


def _real_stdin_buffer() -> object:
    """``sys.stdin``'s buffer, or None when stdin is closed or missing.

    With fd 0 closed the interpreter leaves ``sys.stdin`` as None. Param
    reading must then fall back to empty input, never crash and never
    read fd 0 itself (which the guard may have repurposed).
    """
    stdin = sys.stdin
    if stdin is None:
        return None
    try:
        return stdin.buffer  # type: ignore[attr-defined]
    except AttributeError:
        return None


def cmd_install_commands(
    registry: object,
    args: argparse.Namespace,
    out: object,
    *,
    runner: object = None,
    commands_dir: object = None,
    on_first_write: object = None,
) -> tuple[int, str | None]:
    """Returns ``(exit_code, error_line_or_None)``; prints every step.

    ``on_first_write`` runs immediately before the first ``out`` write
    (never on error paths, which write nothing here): the caller arms
    final-delivery protection only after every ``sudo install`` child
    has finished.
    """
    def _note() -> None:
        if on_first_write is not None:
            on_first_write()  # type: ignore[operator]

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
    # normpath drops a trailing slash, which would make islink() miss a
    # symlinked directory.
    dest = os.path.normpath(dest)
    run = runner if runner is not None else subprocess.run
    # The SDK never creates this directory, so a fresh Pi has no dest
    # yet. Create it root-owned and mode 755 like the SDK requires, and
    # never follow or replace a symlink or file planted at its path.
    if os.path.lexists(dest):
        if os.path.islink(dest) or not os.path.isdir(dest):
            return 1, _error_line(
                "config_invalid", f"{dest} is not a directory"
            )
    elif args.dry_run:
        _note()
        out.write(f"would create {dest} (owner root, mode 755)\n")  # type: ignore[attr-defined]
    else:
        try:
            _run_child(
                run,
                ["sudo", "install", "-d", "-o", "root", "-g", "root",
                 "-m", "755", dest],
            )
        except Exception:
            return 1, _error_line(
                "config_invalid", f"could not create {dest}"
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
        if not args.dry_run:
            for name in names:
                argv = [
                    "sudo", "install", "-o", "root", "-g", "root", "-m", "644",
                    os.path.join(tmpdir, name), os.path.join(dest, name),
                ]
                try:
                    _run_child(run, argv)
                except Exception:
                    return 1, _error_line(
                        "config_invalid", f"could not install {name}"
                    )
        else:
            _note()
            for name in names:
                out.write(f"would install {name} -> {dest}/{name}\n")  # type: ignore[attr-defined]
        _note()
        for name in stale:
            out.write(  # type: ignore[attr-defined]
                f"warning: stale drop-in file {name} is not generated; "
                "left in place\n"
            )
        _note()
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
    seal: bool | None = None,
) -> int:
    if _exit is None:
        _exit = os._exit
    env_map = os.environ if env is None else env
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    stream_in = stdin if stdin is not None else _real_stdin_buffer()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.subcommand is None:
        parser.print_help(out)
        return 0

    # serve is not a Muse command: a long-running read-only dashboard
    # server that logs to the real stderr. It runs exactly as on main,
    # before the guard starts: no guard, no SIGINT override, no
    # watchdog, no seal, no hard exit -- sys streams and fds 1/2 stay
    # untouched and serve_command's code returns normally. The
    # nested-guard and guard-unavailable paths do not apply either.
    if args.subcommand == "serve":
        if registry is None:
            try:
                registry = load_registry()
            except RegistryError as exc:
                err.write(_error_line("config_invalid", str(exc)) + "\n")
                return 1
        from housesitter.core import serve as _serve

        return _serve.serve_command(registry, env_map, out, err)

    # The guard starts after parse_args (argparse's own --help/--version
    # output stays unguarded) and before load_registry (module import-time
    # output is caught). When the caller injected its own streams
    # (in-process tests) the guard restores instead of sealing; otherwise
    # (production entry points) it seals. Pass seal= to force either way.
    if seal is None:
        seal_mode = stdout is None and stderr is None
    else:
        seal_mode = bool(seal)
    try:
        guard = _stdio.start()
    except (OSError, RuntimeError):
        return _guard_unavailable(
            args, parser, out, err, stdout, stderr,
            env_map, stream_in, _exit,
            registry, config, commands_dir, install_runner,
        )
    if guard is not None:
        if stdout is None:
            out = guard.stdout
        if stderr is None:
            err = guard.stderr
    if seal_mode and guard is not None:
        _default_sigint_for_seal()
    # In seal mode (production) the process never returns from here and
    # never re-enters normal interpreter shutdown: the normal path seals
    # the real streams and ends with os._exit(code), and ANY BaseException
    # escaping the guarded body (SystemExit with any code, KeyboardInterrupt,
    # BrokenPipeError, ...) gets fixed diagnostics on the reserved real
    # streams, then seal() and os._exit(1). Re-raising would run module
    # atexit hooks, non-daemon threads, and finalizers on module-reachable
    # streams, and the traceback would land in the voided sink while the
    # real stderr stays empty. Restore mode (in-process use) restores and
    # returns normally, re-raising whatever escaped.
    seal_note: dict = {"output_started": False, "stdout_started": False}
    try:
        code = _main_guarded(
            args, parser, env_map, stream_in, out, err, _exit,
            registry, config, commands_dir, install_runner,
            guard, seal_mode,
            seal_note,
        )
    except BaseException:
        if not seal_mode:
            if guard is not None:
                try:
                    guard.restore()
                except Exception:
                    pass
            raise
        try:
            _arm_seal_watchdog(_exit, seal_note)
            _seal_fail(guard, args, seal_note)
        except BaseException:
            pass
        _exit(1)
    if seal_mode:
        try:
            sealed_ok = guard.seal(
                _error_line(_UNEXPECTED_CODE, _UNEXPECTED_REASON)
            )
        except BaseException:
            sealed_ok = False
        # Exit 0 only if the real streams flushed completely; otherwise
        # the output was lost and seal() already wrote its fixed line.
        _exit(0 if (sealed_ok and code == 0) else 1)
    guard.restore()
    return code


def _seal_fail(
    guard: object,
    args: argparse.Namespace,
    seal_note: dict | None,
) -> None:
    """Best-effort fixed diagnostics then seal; never raises.

    Seal-mode only: a BaseException escaped the guarded body, so normal
    interpreter shutdown (module atexit hooks, non-daemon threads,
    finalizers on module-reachable streams, and the traceback print
    itself) must not run. For invoke/describe/doctor the matching fixed
    error envelope goes to the reserved real stdout unless stdout
    output already started, then the fixed error line goes to the
    reserved real stderr, then seal with the same fixed line so a
    failed final flush still leaves it. Every step is best-effort and
    no exception text is used: it may be untrusted or secret. The
    caller hard-exits 1 no matter what.
    """
    line = _error_line(_UNEXPECTED_CODE, _UNEXPECTED_REASON)
    try:
        if (
            guard is not None
            and args.subcommand in ("invoke", "describe", "doctor")
            and not (seal_note or {}).get("stdout_started")
        ):
            outcome_env = _envelope.build_error(
                _UNEXPECTED_CODE, _UNEXPECTED_REASON
            )
            guard.stdout.write(  # type: ignore[attr-defined]
                _envelope.serialize(outcome_env) + "\n"
            )
            guard.stdout.flush()  # type: ignore[attr-defined]
    except BaseException:
        pass
    try:
        if guard is not None:
            guard.stderr.write(line + "\n")  # type: ignore[attr-defined]
            guard.stderr.flush()  # type: ignore[attr-defined]
    except BaseException:
        pass
    try:
        if guard is not None:
            guard.seal(line)  # type: ignore[attr-defined]
    except BaseException:
        pass


def _guard_unavailable(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    out: object,
    err: object,
    stdout: object,
    stderr: object,
    env_map: dict,
    stream_in: object,
    _exit: object,
    registry: object,
    config: object,
    commands_dir: object,
    install_runner: object,
) -> int:
    """Run without the guard: module commands refuse, others warn.

    The guard failed to start, or one is already active (a nested
    ``cli.main`` call). ``invoke``/``describe``/``doctor`` must not run a
    handler whose stray output would reach Muse unguarded, so they fail
    with ``upstream_unexpected`` without loading the registry or running
    anything. Every other subcommand continues unguarded after one
    warning line. Output goes to the caller's injected streams, or to
    the outer guard's reserved real streams when nested inside one --
    except a nested module-command refusal with no injected streams,
    which discards its envelope and error line instead: writing them to
    the outer real streams would put two JSON documents on Muse's
    stdout. In production (no injected streams and no outer guard) a
    refusal writes to the real fds, so the delivery watchdog is armed
    immediately before it and it ends with a hard exit, as the guarded
    production path does: a stalled reader must not hang the process
    there either. Commands that continue get no watchdog: they run
    unguarded to a normal exit.
    """
    outer = _stdio.active()
    if outer is not None:
        if stdout is None:
            out = outer.stdout
        if stderr is None:
            err = outer.stderr
    # No watchdog for nested calls: a nested refusal discards its output
    # (nothing to stall on), and a timer here would kill the outer
    # process, which may still be running the outer handler.
    # Production guard-down shape: no injected streams and no outer
    # guard, so the writes below go to the real fds.
    production = outer is None and stdout is None and stderr is None
    if args.subcommand in ("invoke", "describe", "doctor"):
        if outer is not None:
            # Nested refusal with nowhere of its own to write: discard,
            # so the outer command's stdout stays one JSON document.
            # Injected streams still receive the refusal.
            if stdout is None:
                out = io.StringIO()
            if stderr is None:
                err = io.StringIO()
        outcome_env = _envelope.build_error(
            "upstream_unexpected", "cannot protect command output"
        )
        if production:
            _arm_seal_watchdog(_exit, None)
        try:
            out.write(_envelope.serialize(outcome_env) + "\n")  # type: ignore[attr-defined]
            out.flush()  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            err.write(  # type: ignore[attr-defined]
                _error_line("upstream_unexpected",
                            "cannot protect command output") + "\n"
            )
            err.flush()  # type: ignore[attr-defined]
        except Exception:
            pass
        if production:
            _exit(1)  # type: ignore[operator]
        return 1
    # No watchdog here: the command continues unguarded and exits
    # normally, as without the guard, and its remaining work (sudo,
    # file writes) may legitimately take longer than the watchdog.
    try:
        err.write(  # type: ignore[attr-defined]
            "housesitter: housesitter: log: output guard unavailable; "
            "continuing without it\n"
        )
    except Exception:
        pass
    return _main_guarded(
        args, parser, env_map, stream_in, out, err, _exit,
        registry, config, commands_dir, install_runner,
        None, False,
    )


def _stray_count(guard: object) -> int:
    """Bytes the guard discarded so far; 0 when unguarded or on error."""
    if guard is None:
        return 0
    try:
        count = int(guard.count())  # type: ignore[attr-defined]
    except Exception:
        return 0
    return max(count, 0)


def _main_guarded(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    env_map: dict,
    stream_in: object,
    out: object,
    err: object,
    _exit: object,
    registry: object,
    config: object,
    commands_dir: object,
    install_runner: object,
    guard: object,
    seal_mode: bool,
    seal_note: dict | None = None,
) -> int:
    if registry is None:
        try:
            registry = load_registry()
        except RegistryError as exc:
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(_error_line("config_invalid", str(exc)) + "\n")  # type: ignore[attr-defined]
            return 1

    if args.subcommand in ("invoke", "describe", "doctor"):
        stdin_outcome = None
        if args.subcommand == "invoke":
            full_name = args.command
            if args.params is not None:
                raw: bytes | str | dict | None = args.params
            elif stream_in is None:
                raw = b""
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
                stray_output=guard.count if guard is not None else None,  # type: ignore[attr-defined]
            )
        # Count only, never content: on success and failure alike, before
        # any error line, so the error line stays the last stderr line.
        # The runner samples its count right after the handler returns;
        # use that value when present so the envelope warning and this
        # log line always agree. Sample the guard directly only for paths
        # the runner never measured (failure/timeout).
        if outcome.stray_bytes is not None:
            stray_bytes = max(outcome.stray_bytes, 0)
        else:
            stray_bytes = _stray_count(guard)
        if stray_bytes > 0:
            if outcome.exit_code == 0:
                stray_module = outcome.envelope.get("module") or "housesitter"
            else:
                stray_module = outcome.error_module or "housesitter"
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(  # type: ignore[attr-defined]
                f"housesitter: {stray_module}: log: the module wrote "
                f"{stray_bytes} bytes outside its result; discarded\n"
            )
        text = _envelope.serialize(outcome.envelope) + "\n"
        # Mark stdout output started BEFORE the first envelope byte:
        # once any stdout write is attempted, a later seal-mode failure
        # must not append a replacement envelope — only the fixed
        # stderr line and exit 1.
        _delivery_started(guard, seal_mode, _exit, seal_note)
        out.write(text)  # type: ignore[attr-defined]
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
            # Seal before the hard exit: os._exit skips the finally above,
            # and late worker output must go nowhere, not to real stdout.
            if guard is not None:
                if seal_mode:
                    guard.seal(  # type: ignore[attr-defined]
                        _error_line(_UNEXPECTED_CODE, _UNEXPECTED_REASON)
                    )
                else:
                    guard.restore()  # type: ignore[attr-defined]
            _exit(1)  # type: ignore[operator]
        return 1

    if args.subcommand == "list":
        try:
            cfg = config if config is not None else load_config(registry, env=env_map)
        except ConfigError as exc:
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(_error_line("config_invalid", f"{exc.key}: {exc.problem}") + "\n")
            return 1
        _delivery_started(guard, seal_mode, _exit, seal_note)
        return cmd_list(registry, cfg, as_json=args.as_json, out=out)

    if args.subcommand == "config-check":
        try:
            cfg = config if config is not None else load_config(registry, env=env_map)
        except ConfigError as exc:
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(_error_line("config_invalid", f"{exc.key}: {exc.problem}") + "\n")
            return 1
        _delivery_started(guard, seal_mode, _exit, seal_note)
        return cmd_config_check(registry, cfg, _builtins.home_of(env_map), out)

    if args.subcommand == "dropins":
        code, line = cmd_dropins(
            registry, args, out,
            on_first_write=lambda: _delivery_started(
                guard, seal_mode, _exit, seal_note),
        )
        if line is not None:
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(line + "\n")
        return code

    if args.subcommand == "install-commands":
        code, line = cmd_install_commands(
            registry, args, out, runner=install_runner,
            commands_dir=commands_dir,
            on_first_write=lambda: _delivery_started(
                guard, seal_mode, _exit, seal_note),
        )
        if line is not None:
            _delivery_started(
                guard, seal_mode, _exit, seal_note, to_stdout=False
            )
            err.write(line + "\n")
        return code

    # Unreachable via parse_args (it rejects bad subcommands itself),
    # but kept as a backstop: unlike parser.error, which writes usage
    # to the real sys.stderr and raises SystemExit, this routes usage
    # and the error line to the caller's (real) stderr and returns 2,
    # so the message can never land in the guarded sink.
    _delivery_started(guard, seal_mode, _exit, seal_note, to_stdout=False)
    try:
        parser.print_usage(err)
    except Exception:
        pass
    try:
        err.write(  # type: ignore[attr-defined]
            f"{parser.prog}: error: unknown subcommand {args.subcommand}\n"
        )
    except Exception:
        pass
    return 2
