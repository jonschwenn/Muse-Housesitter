"""Subprocess entry for the stdio-guard tests (test-only, never installed).

Runs the production CLI path with real file descriptors and production
sealing: it builds a Registry via Registry.from_classes with a test-only
module and calls cli.main WITHOUT injecting stdout/stderr, so the guard
treats this like production (seal at exit). The test captures the real
stdout/stderr pipes and the exit code.

Usage: python tests/stdio_helper.py <home_dir> <scenario>
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from housesitter import sdk
from housesitter.core import cli
from housesitter.core.config import Config, ModuleConfig
from housesitter.core.registry import Registry

MODULE_ID = "stdio_probe"
FULL_NAME = "housesitter.stdio_probe.probe"

MARK_PRINT = "STRAY-MARKER-print-leak"
MARK_FD1 = "STRAY-MARKER-fd-one"
MARK_FD2 = "STRAY-MARKER-dunder-one"
MARK_ERR1 = "STRAY-MARKER-err-one"
MARK_ERR2 = "STRAY-MARKER-err-two"
MARK_SPIN = "STRAY-MARKER-spin"
MARK_LATE = "STRAY-MARKER-late-thread"
MARK_DUNDER = "STRAY-MARKER-dunder-unflushed"
MARK_C = "STRAY-MARKER-c-buffered"
MARK_FAULT = "most recent call"
MARK_SERVE_OUT = "SERVE-MARKER-out-line"
MARK_SERVE_ERR = "SERVE-MARKER-err-line"
MARK_SERVE_FD = "SERVE-MARKER-fd-line"
# serve_command stub's normal return, and its return when it detects the
# guard (redirected streams or SIGINT forced to default).
SERVE_EXIT_CODE = 3
SERVE_GUARD_CODE = 7


def _ok(data):
    return sdk.Result(data=data)


def _run_print_leak(ctx, params):
    print(MARK_PRINT)
    return _ok({"count": 1})


def _run_fd_write(ctx, params):
    os.write(1, (MARK_FD1 + "\n").encode("utf-8"))
    sys.__stdout__.write(MARK_FD2 + "\n")
    sys.__stdout__.flush()
    return _ok({"count": 1})


def _run_stderr_fail(ctx, params):
    sys.stderr.write(MARK_ERR1 + "\n")
    sys.stderr.flush()
    os.write(2, (MARK_ERR2 + "\n").encode("utf-8"))
    raise sdk.CommandError("upstream_unexpected", "plain reason for the failure")


def _run_timeout_spin(ctx, params):
    chunk = (MARK_SPIN + "x" * 512 + "\n").encode("utf-8")
    while True:
        os.write(1, chunk)
        os.write(2, chunk)


def _run_big_stray(ctx, params):
    print("q" * 90000)
    return _ok({"count": 1})


def _run_late_thread(ctx, params):
    def _late():
        time.sleep(1.0)
        os.write(1, (MARK_LATE + "\n").encode("utf-8"))
        os.write(2, (MARK_LATE + "\n").encode("utf-8"))

    thread = threading.Thread(target=_late, daemon=False)
    thread.start()
    return _ok({"count": 1})


def _run_import_lazy(ctx, params):
    import stdio_leaky_import  # noqa: F401  (import prints once, under guard)

    return _ok({"count": 1})


def _run_dunder_unflushed(ctx, params):
    # No flush: sys.__stdout__ is one of the guard's own unbuffered
    # write-through wrappers, so these bytes reach the sink immediately,
    # are counted, and never reach the real stdout.
    sys.__stdout__.write(MARK_DUNDER + "\n")
    return _ok({"count": 1})


def _run_c_buffered(ctx, params):
    # C-level buffered stdio (test helper only, never src/): printf without
    # fflush sits in libc's buffer until process exit, when fds 1/2 are
    # already sealed to /dev/null. The marker must never reach real stdout.
    import ctypes

    libc = None
    for name in ("libc.so.6", None):
        try:
            libc = ctypes.CDLL(name)
        except OSError:
            continue
        break
    if libc is None:  # pragma: no cover - test skips when libc is missing
        raise sdk.CommandError("upstream_unexpected", "no libc available")
    libc.printf((MARK_C + "\n").encode("utf-8"))
    return _ok({"count": 1})


def _run_quiet(ctx, params):
    return _ok({"count": 1})


def _run_sigint_blockers(ctx, params):
    # F2: a sleeping module atexit hook plus a non-daemon thread that
    # outlive the command. With SIGINT at its default Python disposition
    # this shutdown machinery hangs; with the seal-mode SIG_DFL policy
    # the process dies by signal instead.
    import atexit

    atexit.register(lambda: time.sleep(30))
    thread = threading.Thread(target=lambda: time.sleep(30), daemon=False)
    thread.start()
    return _ok({"count": 1})


def _run_big_envelope(ctx, params):
    # F3: a ~16 KB envelope exceeds a 4,096-byte pipe buffer, so delivery
    # to an undrained pipe blocks until the seal watchdog hard-exits.
    return _ok({"count": 1, "blob": "x" * 16000})


def _run_fault_dump(ctx, params):
    # D1: faulthandler.dump_traceback() is one C call that holds the GIL
    # while writing ~146 KB. A pipe sink fills and the drain thread can
    # never run, hanging the process; a memfd sink never blocks.
    import faulthandler

    ready = []
    wait = threading.Event()

    def deep(n):
        if n:
            return deep(n - 1)
        ready.append(1)
        wait.wait(20)

    for _ in range(25):
        threading.Thread(target=deep, args=(80,), daemon=True).start()
    while len(ready) < 25:
        time.sleep(0.01)
    faulthandler.dump_traceback()
    wait.set()
    return _ok({"count": 1})


def _run_c_printf_big(ctx, params):
    # D1: ctypes PyDLL calls hold the GIL across printf, so 90 KB into a
    # full pipe hangs the process. Into a memfd it never blocks.
    import ctypes

    libc = ctypes.PyDLL(None)
    libc.printf(b"x" * 90000)
    return _ok({"count": 1})


def _run_flush_block(ctx, params):
    # D2: the handler swaps in a stdout whose flush() blocks forever, then
    # overruns its deadline. Sampling the count must never flush it.
    original = sys.stdout

    class BlockingFlush:
        def write(self, text):
            return original.write(text)

        def flush(self):
            time.sleep(30)

    sys.stdout = BlockingFlush()
    time.sleep(30)
    return _ok({"count": 1})


def _run_seal_flush_timeout(ctx, params):
    # R1: the handler mutates the EXISTING guard wrapper's flush (the
    # object seal() used to close) and overruns its deadline. Seal must
    # never call it: the timeout envelope arrives and the process exits.
    sys.stdout.flush = lambda: time.sleep(30)  # noqa: E731
    time.sleep(30)
    return _ok({"count": 1})


def _run_seal_flush_success(ctx, params):
    # R1: same mutation on the success path: the envelope arrives and
    # the process still exits instead of hanging in cleanup.
    sys.stdout.flush = lambda: time.sleep(30)  # noqa: E731
    return _ok({"count": 1})


def _run_zombie_thread(ctx, params):
    # R1: a non-daemon handler thread that outlives the command must not
    # keep the process alive: seal mode ends with os._exit.
    def _linger():
        time.sleep(30)

    thread = threading.Thread(target=_linger, daemon=False)
    thread.start()
    return _ok({"count": 1})


def _run_threaded_exact(ctx, params):
    # R2: four threads writing concurrently through one shared sink
    # description must count every byte exactly.
    per_thread = 512
    payload = b"t" * 1024 + b"\n"
    marker = [0] * 4

    def _write(index):
        for _ in range(per_thread):
            marker[index] += os.write(1, payload)

    threads = [
        threading.Thread(target=_write, args=(index,)) for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(marker) == 4 * per_thread * len(payload)
    return _ok({"count": 1})


def _run_blocked_buffer(ctx, params):
    # D2: a worker thread holds a BufferedWriter lock while blocked in its
    # raw writer, then the command overruns its deadline. The count must
    # never take that lock.
    import io

    ready = threading.Event()

    class Blocked(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            ready.set()
            time.sleep(30)
            return len(data)

    sys.stdout = io.TextIOWrapper(io.BufferedWriter(Blocked()))

    def _write():
        try:
            sys.stdout.write("x" * 90000)
        except Exception:
            pass

    threading.Thread(target=_write, daemon=True).start()
    ready.wait(10)
    time.sleep(30)
    return _ok({"count": 1})


_HIGHFD_WANT = 1100


def _open_many_fds():
    """Open many fds before the guard starts; None when impossible."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < 4096:
            try:
                resource.setrlimit(
                    resource.RLIMIT_NOFILE, (min(4096, hard), hard)
                )
            except (OSError, ValueError):
                pass
    except (ImportError, OSError, ValueError):
        pass
    held = []
    try:
        for _ in range(_HIGHFD_WANT):
            held.append(os.open("/dev/null", os.O_RDONLY))
    except OSError:
        for fd in held:
            try:
                os.close(fd)
            except OSError:
                pass
        return None
    return held


def _run_highfd(ctx, params):
    # D3: runs with > 1100 fds already open (see main()). select()-based
    # drains die there; the memfd sink has no fd-number limit.
    print("x" * 90000)
    sys.__stdout__.write("y" * 90000 + "\n")
    return _ok({"count": 1})


# F1/F2: test-side fault injection. Production carries no test hooks,
# so each seal-path scenario drives its fault from this helper process
# through general-purpose seams: a thin wrapper around the guard's
# reserved real stdout (installed via stdio.start), the _exit callable
# passed to cli.main, or a wrapper around OutputGuard.seal.
def _send_sigint_self():
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except Exception:
        pass


def _sigint_then_exit(code):
    # F2: SIGINT just before the hard exit. Seal mode restored SIGINT to
    # its default disposition, so this dies by signal like SIGTERM would.
    _send_sigint_self()
    os._exit(code)


class _PrefixRaiseStdout:
    """F1: write a 100-byte envelope prefix, flush, then raise.

    Thin wrapper around the guard's reserved real stdout: the envelope
    bytes are delivered exactly as the old production-side fault did,
    then an exception escapes _main_guarded after output started, so
    the seal-fail path must not append a replacement envelope.
    """

    def __init__(self, real):
        self._real = real

    def write(self, text):
        self._real.write(text[:100])
        try:
            self._real.flush()
        except Exception:
            pass
        raise RuntimeError("test fault: fail partway through envelope")

    def flush(self):
        return self._real.flush()

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FlushFailThenRaiseStdout:
    """F1: fail the first flush with OSError, deliver later, then raise.

    Thin wrapper around the guard's reserved real stdout. The fault
    happens in flush(), never in write(): the first flush fails with
    OSError (the CLI swallows it, exactly like a production flush
    failure), a later flush delivers the buffered bytes, then a
    RuntimeError escapes so the exit is still 1 without a second
    envelope. Pins the real flush-failure path.
    """

    def __init__(self, real):
        self._real = real
        self._failures_left = 1

    def write(self, text):
        return self._real.write(text)

    def flush(self):
        if self._failures_left > 0:
            self._failures_left -= 1
            raise OSError("injected test flush failure")
        self._real.flush()
        raise RuntimeError("test fault: fail after envelope delivery")

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _RaiseOnWriteStderr:
    """W3: every write to the reserved real stderr raises.

    Thin wrapper around the guard's reserved real stderr. Models a
    stderr that fails (full or closed destination): the CLI's stray
    log line or error line escapes instead of landing, so the
    unexpected-failure path must still deliver the fixed envelope to
    the healthy stdout.
    """

    def __init__(self, real):
        self._real = real

    def write(self, text):
        raise OSError("injected test stderr failure")

    def flush(self):
        return self._real.flush()

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_faults(scenario):
    """Install the test-side fault for one scenario; nothing otherwise."""
    from housesitter.core import stdio as _stdio_mod

    if scenario in ("partial_prefix_raise", "flushfail_then_raise"):
        wrapper_cls = (
            _PrefixRaiseStdout
            if scenario == "partial_prefix_raise"
            else _FlushFailThenRaiseStdout
        )
        real_start = _stdio_mod.start

        def fault_start():
            guard = real_start()
            if guard is not None and guard.stdout is not None:
                guard.stdout = wrapper_cls(guard.stdout)
            return guard

        _stdio_mod.start = fault_start
    elif scenario == "sigint_during_delivery":
        # F2: SIGINT during final delivery: send it just before the real
        # seal runs. The envelope was already flushed, so stdout keeps
        # its one object while the process dies by signal.
        real_seal = _stdio_mod.OutputGuard.seal

        def sigint_seal(self, *args, **kwargs):
            _send_sigint_self()
            return real_seal(self, *args, **kwargs)

        _stdio_mod.OutputGuard.seal = sigint_seal
    elif scenario in ("guard_down", "guard_down_slow_install"):
        # W2: the guard cannot start at all in production shape.
        def _boom():
            raise OSError("injected guard failure")

        _stdio_mod.start = _boom
    elif scenario in (
        "stray_stderr_raise", "registry_stderr_raise", "stray_ok_stderr_raise"
    ):
        # W3: the reserved real stderr fails every write, while stdout
        # stays healthy.
        real_start = _stdio_mod.start

        def fault_start():
            guard = real_start()
            if guard is not None and guard.stderr is not None:
                guard.stderr = _RaiseOnWriteStderr(guard.stderr)
            return guard

        _stdio_mod.start = fault_start


_SCENARIOS = {
    "print_leak": _run_print_leak,
    "fd_write": _run_fd_write,
    "stderr_fail": _run_stderr_fail,
    "timeout_spin": _run_timeout_spin,
    "big_stray": _run_big_stray,
    "late_thread": _run_late_thread,
    "import_lazy": _run_import_lazy,
    "dunder_unflushed": _run_dunder_unflushed,
    "c_buffered": _run_c_buffered,
    "quiet": _run_quiet,
    "big_envelope": _run_big_envelope,
    "sigint_blockers": _run_sigint_blockers,
    "sigint_before_exit": _run_sigint_blockers,
    "sigint_during_delivery": _run_sigint_blockers,
    "flushfail_then_raise": _run_quiet,
    "partial_prefix_raise": _run_quiet,
    "slow_install": _run_quiet,
    "slow_check": _run_quiet,
    "guard_down": _run_quiet,
    "guard_down_slow_install": _run_quiet,
    "stray_stderr_raise": _run_stderr_fail,
    "stray_ok_stderr_raise": _run_print_leak,
    "registry_stderr_raise": _run_quiet,
    "fault_dump": _run_fault_dump,
    "c_printf_big": _run_c_printf_big,
    "flush_block": _run_flush_block,
    "blocked_buffer": _run_blocked_buffer,
    "highfd": _run_highfd,
    "seal_flush_timeout": _run_seal_flush_timeout,
    "seal_flush_success": _run_seal_flush_success,
    "zombie_thread": _run_zombie_thread,
    "threaded_exact": _run_threaded_exact,
}


def _env_seconds(name, default):
    """Test-only override for a timing constant; production keeps default.

    Decision 7 (option A): most watchdog-fires tests run the helper with
    ``HOUSESITTER_STDIO_WATCHDOG_S=1`` so the seal watchdog fires after
    one second instead of five; slow-work tests add
    ``HOUSESITTER_STDIO_SLOW_S=1.6`` so the slow sleep still outlives it.
    Non-positive or unparsable values keep the production default.
    """
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def _log_seal_watchdog(path):
    """Test-only: record when the CLI's watchdog timers arm and fire.

    Appends ``armed <t>`` / ``fired <t>`` lines (``time.monotonic()``) to ``path`` so a
    test can measure the real watchdog interval inside this process,
    independent of process start-up or parent scheduling. Only the CLI
    module's view of ``threading`` is shadowed; nothing else changes.
    """

    def _note(label):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, f"{label} {time.monotonic():.6f}\n".encode())
        finally:
            os.close(fd)

    real_timer = threading.Timer

    class _LoggedTimer(real_timer):
        def __init__(self, interval, function, args=None, kwargs=None):
            def _fire(*a, **k):
                _note("fired")
                return function(*a, **k)

            super().__init__(interval, _fire, args, kwargs)

        def start(self):
            _note("armed")
            super().start()

    shadow = types.SimpleNamespace(**vars(threading))
    shadow.Timer = _LoggedTimer
    cli.threading = shadow


def _run_serve_probe(home_dir):
    """Stubbed ``serve``: the console must be exactly as on ``main``.

    Replaces ``serve_command`` with a stub that refuses to run guarded:
    it returns ``SERVE_GUARD_CODE`` when ``sys.stdout``/``sys.stderr``
    are not the originals or SIGINT is not Python's default handler,
    and otherwise writes one line each via ``sys.stderr.write``,
    ``print``, and fd 2, sleeps past the (shrunk) seal watchdog
    interval, and returns ``SERVE_EXIT_CODE``. A correct exemption
    delivers all three lines to the real streams and exits with the
    stub's code; a guarded ``serve`` swallows the lines and hard-exits.
    """
    from housesitter.core import serve as _serve_mod

    orig_out, orig_err = sys.stdout, sys.stderr
    # Any watchdog would use this interval: the stub's sleep outlasts
    # it, so an armed watchdog would kill the process mid-sleep.
    cli._SEAL_WATCHDOG_S = 1

    def _stub(registry, env, out, err):
        if (
            sys.stdout is not orig_out
            or sys.stderr is not orig_err
            or signal.getsignal(signal.SIGINT) != signal.default_int_handler
        ):
            return SERVE_GUARD_CODE
        sys.stderr.write(MARK_SERVE_ERR + "\n")
        sys.stderr.flush()
        print(MARK_SERVE_OUT)
        sys.stdout.flush()
        os.write(2, (MARK_SERVE_FD + "\n").encode("utf-8"))
        time.sleep(3)
        return SERVE_EXIT_CODE

    _serve_mod.serve_command = _stub
    module_cls = make_module("quiet")
    registry = Registry.from_classes([module_cls])
    config = Config(
        path=os.path.join(home_dir, "config.json"),
        secrets_dir=os.path.join(home_dir, "secrets"),
        identifiers="full",
        retention=500,
        modules={MODULE_ID: ModuleConfig(enabled=True, retention=500)},
    )
    return cli.main(
        ["serve"],
        registry=registry,
        env={"HOME": home_dir},
        config=config,
    )


def make_module(scenario):
    handler = _SCENARIOS[scenario]

    def probe(self, ctx, params):
        return handler(ctx, params)

    timeout_ms = 5000 if scenario in (
        "timeout_spin", "fault_dump", "c_printf_big",
        "flush_block", "blocked_buffer", "seal_flush_timeout",
    ) else 30000
    fields = (sdk.Field("data.count", "integer"),)
    if scenario == "big_envelope":
        # Declared (else the runner drops it) and wide enough to hold
        # the ~16 KB payload that overfills a 4,096-byte test pipe.
        fields = fields + (
            sdk.Field("data.blob", "string", max_len=20000),
        )
    command = sdk.Command(
        name="probe",
        handler="probe",
        description="Read a probe value from the test sensor.",
        fields=fields,
        timeout_ms=timeout_ms,
    )
    members = {
        "id": MODULE_ID,
        "title": "Stdio probe",
        "version": "0.1.0",
        "config": {},
        "http_allow": (),
        "commands": (command,),
        "probe": probe,
    }
    if scenario == "slow_check":
        # W1: doctor --check runs this read-only module check in the
        # worker thread; it takes longer than the final-delivery
        # watchdog (7 s vs 5 s production, or SLOW_S vs 1 s fast),
        # well within the 60 s doctor deadline.
        def check(self, ctx):
            time.sleep(_env_seconds("HOUSESITTER_STDIO_SLOW_S", 7))
            return ["check-ok-example"]

        members["check"] = check
    return type("StdioProbeModule", (sdk.Module,), members)


def main(argv):
    home_dir, scenario = argv[1], argv[2]
    # E3: which production subcommand to run. Default "invoke" keeps
    # every existing caller on the invoke path.
    cmd_key = argv[3] if len(argv) > 3 else "invoke"
    # Decision 7 (option A): test-only shortened seal watchdog. The
    # production default (5 s) is untouched; only helper runs with this
    # env var set arm an earlier watchdog. Unset keeps production timing.
    cli._SEAL_WATCHDOG_S = _env_seconds(
        "HOUSESITTER_STDIO_WATCHDOG_S", cli._SEAL_WATCHDOG_S
    )
    watchdog_log = os.environ.get("HOUSESITTER_STDIO_WATCHDOG_LOG")
    if watchdog_log:
        _log_seal_watchdog(watchdog_log)
    # D3: hold > 1100 fds open BEFORE the guard starts, so the guard's
    # own fds land above FD_SETSIZE. Kept alive in this frame for the
    # whole run. None means the platform refused: skip with exit 3.
    held_fds = None
    if scenario == "highfd":
        held_fds = _open_many_fds()
        if held_fds is None:
            sys.stdout.write("HIGHFD-SKIP\n")
            sys.stdout.flush()
            return 3
    assert held_fds is None or len(held_fds) == _HIGHFD_WANT
    # E1(d): a blocking module atexit hook plus a non-daemon thread that
    # outlive the command. Old seal+re-raise shutdown hung on these.
    if scenario == "body_raise_atexit_thread":
        import atexit

        atexit.register(lambda: time.sleep(30))
        thread = threading.Thread(target=lambda: time.sleep(30), daemon=False)
        thread.start()
    # E2: a KeyboardInterrupt surfaces from the first reserved-real
    # flush after the trim join -- exactly where a SIGINT landing
    # mid-flush would surface. One-shot on the guard instance (the C
    # type itself is immutable, so the class cannot be patched):
    # later flushes (the fixed error line) behave normally.
    if scenario == "seal_sigint":
        from housesitter.core import stdio as _stdio_mod

        real_join = _stdio_mod.OutputGuard._join_trim
        flush_state = {"armed": False}

        def joined(self):
            real_join(self)
            if not getattr(self, "_sig_sent", False):
                self._sig_sent = True
                flush_state["armed"] = True
                real_flushes = {}
                for name in ("stdout", "stderr"):
                    stream = getattr(self, name, None)
                    if stream is None:
                        continue
                    real_flushes[name] = stream.flush

                    def boom(*args, _name=name, **kwargs):
                        if flush_state["armed"]:
                            flush_state["armed"] = False
                            raise KeyboardInterrupt(
                                "injected SIGINT during final flush"
                            )
                        return real_flushes[_name](*args, **kwargs)

                    try:
                        stream.flush = boom
                    except (AttributeError, TypeError):
                        pass

        _stdio_mod.OutputGuard._join_trim = joined
        scenario = "quiet"
    # E1(b,d): the guarded body raises instead of returning an Outcome.
    if scenario in ("body_raise", "body_raise_atexit_thread"):
        def _broken(*args, **kwargs):
            raise RuntimeError("simulated unexpected failure")

        cli._runner.run_command = _broken
        scenario = "quiet"
    if cmd_key == "serve":
        return _run_serve_probe(home_dir)
    if os.environ.get("HOUSESITTER_STDIO_NO_PARAMS") == "1":
        invoke_argv = ["invoke", FULL_NAME]
    else:
        invoke_argv = ["invoke", FULL_NAME, "--params", "{}"]
    config = Config(
        path=os.path.join(home_dir, "config.json"),
        secrets_dir=os.path.join(home_dir, "secrets"),
        identifiers="full",
        retention=500,
        modules={MODULE_ID: ModuleConfig(enabled=True, retention=500)},
    )
    # E1(a): a plugin import raising SystemExit(0) under load_registry.
    if scenario == "import_exit0":
        def lazy_registry_exit():
            import stdio_exit_import  # noqa: F401 (raises SystemExit(0))

            raise AssertionError("unreachable: import must raise SystemExit")

        cli.load_registry = lazy_registry_exit
        return cli.main(
            invoke_argv,
            registry=None,
            env={"HOME": home_dir},
            config=config,
        )
    if scenario == "import_registry":
        # Load the registry (which imports a printing module) only AFTER
        # the guard is up, by passing registry=None like production.
        module_cls = make_module("quiet")
        real_load = cli.load_registry

        def lazy_registry():
            import stdio_leaky_import  # noqa: F401  (prints once, under guard)

            assert real_load is not None
            return Registry.from_classes([module_cls])

        cli.load_registry = lazy_registry
        return cli.main(
            invoke_argv,
            registry=None,
            env={"HOME": home_dir},
            config=config,
        )
    if scenario == "registry_stderr_raise":
        # W3: the registry fails to load, so the config_invalid error
        # line is the first CLI write.
        def _bad_registry():
            from housesitter.core.registry import RegistryError

            raise RegistryError("injected registry failure")

        cli.load_registry = _bad_registry
    module_cls = make_module(scenario)
    registry = Registry.from_classes([module_cls])
    if scenario == "registry_stderr_raise":
        # Pass no registry so the CLI loads it (and fails) itself.
        registry = None
    if scenario in ("slow_install", "guard_down_slow_install"):
        # guard_down_slow_install: the same slow install with the guard
        # unable to start (the unguarded continue path).
        _install_faults(scenario)
        # W1: a slow installer backend; only the first file sleeps so
        # one run stays well under 20 s production (well under 10 s
        # fast). Uses the injectable runner seam (an interactive sudo
        # password prompt behaves the same way: the child takes longer
        # than the delivery watchdog).
        slept = []

        def _slow_run(argv, **kwargs):
            if not slept:
                slept.append(True)
                time.sleep(_env_seconds("HOUSESITTER_STDIO_SLOW_S", 7))
            return subprocess.CompletedProcess(argv, 0)

        return cli.main(
            [
                "install-commands", "--exec", "/usr/bin/housesitter",
                "--commands-dir", os.path.join(home_dir, "dest"),
            ],
            registry=registry,
            env={"HOME": home_dir},
            config=config,
            install_runner=_slow_run,
        )
    # E3: production subcommand matrix. Fixed exec path and dirs keep
    # every byte deterministic across runs and machines.
    if cmd_key == "invoke":
        cmd_argv = invoke_argv
    elif cmd_key == "list":
        cmd_argv = ["list"]
    elif cmd_key == "list-json":
        cmd_argv = ["list", "--json"]
    elif cmd_key == "config-check":
        cmd_argv = ["config-check"]
    elif cmd_key == "dropins-print":
        cmd_argv = ["dropins", "--print", "--exec", "/usr/bin/housesitter"]
    elif cmd_key == "dropins-out":
        cmd_argv = [
            "dropins", "--out", os.path.join(home_dir, "dropins"),
            "--exec", "/usr/bin/housesitter",
        ]
    elif cmd_key == "dry-run":
        cmd_argv = [
            "install-commands", "--dry-run", "--exec", "/usr/bin/housesitter",
            "--commands-dir", os.path.join(home_dir, "dest"),
        ]
    elif cmd_key == "describe":
        cmd_argv = ["describe"]
    elif cmd_key == "doctor":
        cmd_argv = ["doctor"]
    elif cmd_key == "doctor-check":
        cmd_argv = ["doctor", "--check"]
    else:
        raise AssertionError(f"unknown helper command {cmd_key!r}")
    # F1/F2: install the test-side fault for this scenario (a no-op for
    # every other scenario), then run the production CLI path. The helper
    # is a fresh process per run so nothing leaks between tests.
    _install_faults(scenario)
    if scenario == "sigint_before_exit":
        return cli.main(
            cmd_argv,
            registry=registry,
            env={"HOME": home_dir},
            config=config,
            _exit=_sigint_then_exit,
        )
    return cli.main(
        cmd_argv,
        registry=registry,
        env={"HOME": home_dir},
        config=config,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
