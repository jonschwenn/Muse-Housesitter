"""The stdio guard: stray handler output never reaches real stdout/stderr.

Most tests run the CLI as a real subprocess (real fds, real exit, real
sealing) through tests/stdio_helper.py. The unit tests at the end pin the
guard's counter directly.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import stdio_helper
from housesitter import sdk
from housesitter.core import envelope as _envelope

HELPER = Path(stdio_helper.__file__)
FULL = stdio_helper.FULL_NAME

WARNING_TEMPLATE = (
    "The module wrote {n} bytes to stdout or stderr outside its result; "
    "Housesitter discarded them."
)

STRAY_LINE_RE = re.compile(
    r"^housesitter: [a-z][a-z0-9_]*: log: the module wrote (\d+) bytes "
    r"outside its result; discarded$"
)


def _close_fd_zero():
    # preexec_fn: run after fork, before exec. Closes stdin so the helper
    # starts with fd 0 closed (POSIX only).
    try:
        os.close(0)
    except OSError:
        pass


def _home_for(tmp_path, scenario, cmd):
    if cmd == "invoke":
        return tmp_path / f"home-{scenario}"
    return tmp_path / f"home-{scenario}-{cmd}"


def run_helper(tmp_path, scenario, *, no_params=False, close_stdin=False,
               cmd="invoke"):
    home = _home_for(tmp_path, scenario, cmd)
    home.mkdir(exist_ok=True)
    env = dict(os.environ)
    if no_params:
        env["HOUSESITTER_STDIO_NO_PARAMS"] = "1"
    kwargs = {}
    if close_stdin:
        kwargs["stdin"] = None
        kwargs["preexec_fn"] = _close_fd_zero
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    proc = subprocess.run(
        [sys.executable, str(HELPER), str(home), scenario, cmd],
        capture_output=True,
        env=env,
        timeout=60,
        **kwargs,
    )
    return proc


def run_helper_limited(tmp_path, scenario, timeout, cmd="invoke"):
    """Run one helper scenario, FAILING if it overruns its deadline.

    A hang past the command deadline plus margin is the bug (the old
    pipe sink blocked GIL-holding writers and blocking flushes), so the
    subprocess timeout fails the test instead of waiting 60 s. The
    whole process group is killed: a timed-out worker keeps running.
    """
    home = _home_for(tmp_path, scenario, cmd)
    home.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, str(HELPER), str(home), scenario, cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        out, err = proc.communicate()
        pytest.fail(
            f"scenario {scenario!r} did not finish within {timeout}s: "
            "the process hung past its command deadline"
        )
    return out, err, proc.returncode


def stray_line(stderr_text):
    for line in stderr_text.splitlines():
        match = STRAY_LINE_RE.match(line)
        if match:
            return line, int(match.group(1))
    return None, 0


class TestPrintLeak:
    def test_print_stays_out_of_stdout_and_warns_first(self, tmp_path):
        proc = run_helper(tmp_path, "print_leak")
        assert proc.returncode == 0
        expected = len((stdio_helper.MARK_PRINT + "\n").encode("utf-8"))
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert stdio_helper.MARK_PRINT not in proc.stdout.decode("utf-8")
        assert stdio_helper.MARK_PRINT not in proc.stderr.decode("utf-8")
        line, count = stray_line(proc.stderr.decode("utf-8"))
        assert count == expected

    def test_fd_and_dunder_writes_are_counted(self, tmp_path):
        proc = run_helper(tmp_path, "fd_write")
        assert proc.returncode == 0
        part1 = (stdio_helper.MARK_FD1 + "\n").encode("utf-8")
        part2 = (stdio_helper.MARK_FD2 + "\n").encode("utf-8")
        expected = len(part1) + len(part2)
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert stdio_helper.MARK_FD1 not in proc.stdout.decode("utf-8")
        assert stdio_helper.MARK_FD2 not in proc.stdout.decode("utf-8")
        assert stdio_helper.MARK_FD1 not in proc.stderr.decode("utf-8")
        assert stdio_helper.MARK_FD2 not in proc.stderr.decode("utf-8")


class TestStderrAndFailure:
    def test_stderr_writes_then_error_line_is_last(self, tmp_path):
        proc = run_helper(tmp_path, "stderr_fail")
        assert proc.returncode == 1
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is False
        # G2: failure envelopes carry no stray-output warning; the count
        # travels only on the stderr log line.
        assert "warnings" not in body
        lines = proc.stderr.decode("utf-8").splitlines()
        assert lines[-1] == (
            "housesitter: stdio_probe: upstream_unexpected: "
            "plain reason for the failure"
        )
        assert stdio_helper.MARK_ERR1 not in proc.stderr.decode("utf-8")
        assert stdio_helper.MARK_ERR2 not in proc.stderr.decode("utf-8")
        assert stdio_helper.MARK_ERR1 not in proc.stdout.decode("utf-8")
        line, count = stray_line(proc.stderr.decode("utf-8"))
        assert line is not None and count > 0
        assert lines.index(line) < len(lines) - 1


class TestTimeout:
    def test_late_writes_never_reach_real_streams(self, tmp_path):
        proc = run_helper(tmp_path, "timeout_spin")
        assert proc.returncode == 1
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body == {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "the command did not finish within its time limit",
            },
        }
        # G2: timeout envelopes carry no stray-output warning either.
        assert "warnings" not in body
        assert stdio_helper.MARK_SPIN.encode() not in proc.stdout
        assert stdio_helper.MARK_SPIN.encode() not in proc.stderr
        err_text = proc.stderr.decode("utf-8")
        lines = err_text.splitlines()
        assert lines[-1].startswith("housesitter: stdio_probe: timeout: ")
        line, count = stray_line(err_text)
        assert line is not None and count > 0
        # The log: count line comes before the error line, which stays last.
        error_index = next(
            index for index, text in enumerate(lines)
            if text.startswith("housesitter: stdio_probe: timeout: ")
        )
        assert error_index == len(lines) - 1
        assert lines.index(line) < error_index


class TestBudget:
    def test_big_stray_output_stays_within_budget(self, tmp_path):
        proc = run_helper(tmp_path, "big_stray")
        assert proc.returncode == 0
        text = proc.stdout.decode("utf-8")
        body = json.loads(text)
        assert body["ok"] is True
        assert _envelope.measure(text) <= _envelope.BUDGET_BYTES
        assert b"q" * 5000 not in proc.stdout
        assert b"q" * 5000 not in proc.stderr
        expected = len(b"q" * 90000 + b"\n")
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        line, count = stray_line(proc.stderr.decode("utf-8"))
        assert line is not None and count == expected


class TestImportTime:
    def test_lazy_import_print_is_caught_and_counted(self, tmp_path):
        proc = run_helper(tmp_path, "import_lazy")
        assert proc.returncode == 0
        expected = len(b"STRAY-MARKER-import-time-leak\n")
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert "STRAY-MARKER-import-time-leak" not in proc.stdout.decode("utf-8")
        assert "STRAY-MARKER-import-time-leak" not in proc.stderr.decode("utf-8")


class TestLateThread:
    def test_thread_writing_after_return_reaches_nowhere(self, tmp_path):
        proc = run_helper(tmp_path, "late_thread")
        assert proc.returncode == 0
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert stdio_helper.MARK_LATE.encode() not in proc.stdout
        assert stdio_helper.MARK_LATE.encode() not in proc.stderr


class TestWarningCap:
    def test_stray_warning_survives_the_warning_cap(self, tmp_path):
        from housesitter.core import runner
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry

        def noisy(self, ctx, params):
            print("STRAY-MARKER-cap-probe")
            return sdk.Result(
                data={"count": 1},
                warnings=[f"note {index}" for index in range(25)],
            )

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "CapProbeModule",
            (sdk.Module,),
            {
                "id": "cap_probe",
                "title": "Cap probe",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": lambda self, ctx, params: noisy(self, ctx, params),
            },
        )
        registry = Registry.from_classes([module_cls])
        config = Config(
            path=str(tmp_path / "config.json"),
            secrets_dir=str(tmp_path / "secrets"),
            identifiers="full",
            retention=500,
            modules={"cap_probe": ModuleConfig(enabled=True, retention=500)},
        )
        outcome = runner.run_command(
            "housesitter.cap_probe.probe",
            {},
            registry=registry,
            env={"HOME": str(tmp_path)},
            config=config,
            log_stream=io.StringIO(),
            stray_output=lambda: 24,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["warnings"][0] == WARNING_TEMPLATE.format(n=24)
        assert len(outcome.envelope["warnings"]) <= 20


class TestGuardCounter:
    def test_exact_count_after_writes(self):
        from housesitter.core import stdio

        guard = stdio.start()
        try:
            os.write(1, b"abc")
            print("hi")
            sys.stderr.write("e")
            sys.stderr.flush()
            assert guard.count() == 3 + 3 + 1
        finally:
            guard.restore()
        assert sys.stdout is not None

    def test_no_block_on_large_writes(self):
        from housesitter.core import stdio

        guard = stdio.start()
        try:
            want = 0
            chunk = b"r" * 65536
            for _ in range(4):
                view = chunk
                while view:
                    done = os.write(1, view)
                    view = view[done:]
                want += len(chunk)
            assert guard.count() == want
        finally:
            guard.restore()

    def test_closed_fds_at_start_are_handled(self):
        if not hasattr(os, "fork"):
            pytest.skip("needs os.fork")
        from housesitter.core import stdio

        pid = os.fork()
        if pid == 0:  # child: mangled fds, then a straight line to _exit
            try:
                os.close(1)
            except OSError:
                pass
            try:
                os.close(2)
            except OSError:
                pass
            try:
                guard = stdio.start()
                os.write(1, b"abc")
                ok = guard.count() == 3
                guard.restore()
                os._exit(0 if ok else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"

    def test_restore_returns_original_streams(self, capsys):
        from housesitter.core import stdio

        before_out, before_err = sys.stdout, sys.stderr
        guard = stdio.start()
        try:
            assert sys.stdout is not before_out
        finally:
            guard.restore()
        assert sys.stdout is before_out
        assert sys.stderr is before_err
        print("after-restore-marker")
        assert "after-restore-marker" in capsys.readouterr().out


def _fd_identity(fd):
    st = os.fstat(fd)
    return (st.st_ino, st.st_dev)


def _open_fd_set():
    return set(os.listdir("/proc/self/fd"))


def _needs_fork():
    if not hasattr(os, "fork"):
        pytest.skip("needs os.fork")


def _needs_proc_self_fd():
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc/self/fd")


class TestStartPartialFailure:
    """B1: ANY failure after the first redirect restores fds and streams.

    The memfd design's start sequence is: dup(1), dup(2) for the saved
    streams, memfd_create for the sink, a /proc reopen, dup2 onto 1
    and 2, two dups for the reserved real streams, two dups for the
    sink wrappers, one dup for the trim thread's private fd, then the
    trim thread. Every point is covered: seven dups, one memfd_create,
    two dup2s, plus the thread start below. (There is no pipe anymore,
    and close failures are swallowed by design, so those kinds are
    gone. The /proc reopen and the open/TextIOWrapper wrapper paths
    have their own rollback tests.)
    """

    KINDS = (
        [("dup", index) for index in range(1, 8)]
        + [("memfd", 1), ("dup2", 1), ("dup2", 2)]
    )

    @pytest.mark.parametrize("kind,index", KINDS)
    def test_start_failure_restores_everything(self, monkeypatch, kind, index):
        from housesitter.core import stdio

        _needs_proc_self_fd()
        real_dup, real_dup2, real_memfd = (
            os.dup, os.dup2, os.memfd_create,
        )
        state = {"dup": 0, "dup2": 0}

        def dup(*args):
            state["dup"] += 1
            if kind == "dup" and state["dup"] == index:
                raise OSError("injected dup failure")
            return real_dup(*args)

        def dup2(*args):
            state["dup2"] += 1
            if kind == "dup2" and state["dup2"] == index:
                raise OSError("injected dup2 failure")
            return real_dup2(*args)

        def memfd_create(*args):
            if kind == "memfd":
                raise OSError("injected memfd failure")
            return real_memfd(*args)

        monkeypatch.setattr(os, "dup", dup)
        monkeypatch.setattr(os, "dup2", dup2)
        monkeypatch.setattr(os, "memfd_create", memfd_create)

        before_fds = _open_fd_set()
        before_out, before_err = sys.stdout, sys.stderr
        before_dunder = (sys.__stdout__, sys.__stderr__)
        before_1, before_2 = _fd_identity(1), _fd_identity(2)
        assert stdio.active() is None
        with pytest.raises(OSError):
            stdio.start()
        assert stdio.active() is None
        assert sys.stdout is before_out
        assert sys.stderr is before_err
        assert (sys.__stdout__, sys.__stderr__) == before_dunder
        assert _fd_identity(1) == before_1
        assert _fd_identity(2) == before_2
        assert _open_fd_set() == before_fds

    def test_thread_start_failure_restores_everything(self, monkeypatch):
        from housesitter.core import stdio

        _needs_proc_self_fd()

        def boom(self):
            raise RuntimeError("injected thread failure")

        monkeypatch.setattr(threading.Thread, "start", boom)
        before_fds = _open_fd_set()
        before_out, before_err = sys.stdout, sys.stderr
        before_1, before_2 = _fd_identity(1), _fd_identity(2)
        assert stdio.active() is None
        with pytest.raises(RuntimeError):
            stdio.start()
        assert stdio.active() is None
        assert sys.stdout is before_out
        assert sys.stderr is before_err
        assert _fd_identity(1) == before_1
        assert _fd_identity(2) == before_2
        assert _open_fd_set() == before_fds


class TestClosedStdin:
    """S1: no guard fd is ever 0, 1, or 2; closed stdin stays clean."""

    def test_guard_fds_avoid_standard_fds(self):
        _needs_fork()
        from housesitter.core import stdio

        pid = os.fork()
        if pid == 0:  # child: fds 0/1/2 closed, then straight to _exit
            try:
                for fd in (0, 1, 2):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                guard = stdio.start()
                try:
                    fds = [
                        guard._saved_out, guard._saved_err, guard._sink_fd,
                        guard.real_stdout_fd, guard.real_stderr_fd,
                        guard.stdout.fileno(), guard.stderr.fileno(),
                        guard._sink_out.fileno(), guard._sink_err.fileno(),
                    ]
                    ok = all(fd >= 3 for fd in fds)
                    # Closed fd 0 reads EOF, never data from real stdout.
                    ok = ok and os.read(0, 1024) == b""
                finally:
                    guard.restore()
                os._exit(0 if ok else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"

    def test_invoke_without_params_closed_stdin(self, tmp_path):
        _needs_fork()
        proc = run_helper(
            tmp_path, "print_leak", no_params=True, close_stdin=True
        )
        assert proc.returncode == 0
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True


class TestDunderUnflushed:
    """S3: bytes buffered in sys.__stdout__ are counted, never leaked."""

    def test_unflushed_dunder_writes_are_counted(self, tmp_path):
        proc = run_helper(tmp_path, "dunder_unflushed")
        assert proc.returncode == 0
        expected = len((stdio_helper.MARK_DUNDER + "\n").encode("utf-8"))
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert stdio_helper.MARK_DUNDER not in proc.stdout.decode("utf-8")
        assert stdio_helper.MARK_DUNDER not in proc.stderr.decode("utf-8")


class TestGuardUnavailable:
    """S4: when the guard cannot start, module commands refuse to run."""

    @staticmethod
    def _boom(monkeypatch):
        from housesitter.core import stdio as stdio_mod

        def boom():
            raise OSError("injected guard failure")

        monkeypatch.setattr(stdio_mod, "start", boom)

    def _registry(self, handler):
        from housesitter.core.registry import Registry

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "GuardProbeModule",
            (sdk.Module,),
            {
                "id": "guard_probe",
                "title": "Guard probe",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": handler,
            },
        )
        return module_cls, Registry.from_classes([module_cls])

    def test_invoke_refuses_without_running_handler(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod
        from housesitter.core import envelope as envelope_mod

        self._boom(monkeypatch)

        def no_load():
            raise AssertionError("registry must not load when guard is down")

        monkeypatch.setattr(cli_mod, "load_registry", no_load)
        calls = []

        def probe(self, ctx, params):
            calls.append(params)
            return sdk.Result(data={"count": 1})

        self._registry(probe)
        out, err = io.StringIO(), io.StringIO()
        code = cli_mod.main(
            ["invoke", "housesitter.guard_probe.probe", "--params", "{}"],
            env={"HOME": str(tmp_path)},
            stdin=io.BytesIO(b""),
            stdout=out,
            stderr=err,
            _exit=no_exit,
            registry=None,
            config=None,
        )
        assert calls == []
        assert code == 1
        expected_env = envelope_mod.build_error(
            "upstream_unexpected", "cannot protect command output"
        )
        assert out.getvalue() == envelope_mod.serialize(expected_env) + "\n"
        assert err.getvalue().splitlines()[-1] == (
            "housesitter: housesitter: upstream_unexpected: "
            "cannot protect command output"
        )

    def test_nested_main_refuses(self, tmp_path):
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod

        def probe(self, ctx, params):
            return sdk.Result(data={"count": 1})

        registry = self._registry(probe)[1]
        guard = stdio_mod.start()
        try:
            out, err = io.StringIO(), io.StringIO()
            code = cli_mod.main(
                ["invoke", "housesitter.guard_probe.probe",
                 "--params", "{}"],
                env={"HOME": str(tmp_path)},
                stdin=io.BytesIO(b""),
                stdout=out,
                stderr=err,
                _exit=no_exit,
                registry=registry,
                config=None,
            )
        finally:
            guard.restore()
        assert code == 1
        assert json.loads(out.getvalue())["error"]["code"] == (
            "upstream_unexpected"
        )
        assert err.getvalue().splitlines()[-1] == (
            "housesitter: housesitter: upstream_unexpected: "
            "cannot protect command output"
        )

    def test_list_continues_unguarded_with_warning(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod
        from housesitter.core.config import Config, ModuleConfig

        self._boom(monkeypatch)

        def probe(self, ctx, params):
            return sdk.Result(data={"count": 1})

        registry = self._registry(probe)[1]
        config = Config(
            path=str(tmp_path / "config.json"),
            secrets_dir=str(tmp_path / "secrets"),
            identifiers="full",
            retention=500,
            modules={"guard_probe": ModuleConfig(enabled=True, retention=500)},
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_mod.main(
            ["list"],
            env={"HOME": str(tmp_path)},
            stdin=io.BytesIO(b""),
            stdout=out,
            stderr=err,
            _exit=no_exit,
            registry=registry,
            config=config,
        )
        assert code == 0
        lines = err.getvalue().splitlines()
        assert lines[0] == (
            "housesitter: housesitter: log: output guard unavailable; "
            "continuing without it"
        )
        assert "housesitter.guard_probe.probe" in out.getvalue()


def no_exit(code):
    raise AssertionError(f"unexpected hard exit({code})")


class TestRunChild:
    """S5: guarded children inherit the real streams; no kwargs fallback."""

    def test_guarded_child_output_reaches_real_streams(self, tmp_path):
        _needs_fork()
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod

        out_path = tmp_path / "child-out.txt"
        err_path = tmp_path / "child-err.txt"
        out_path.write_text("")
        err_path.write_text("")
        pid = os.fork()
        if pid == 0:  # child: fds 1/2 to files, then straight to _exit
            try:
                f1 = os.open(
                    str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                f2 = os.open(
                    str(err_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                os.dup2(f1, 1)
                os.dup2(f2, 2)
                os.close(f1)
                os.close(f2)
                guard = stdio_mod.start()
                try:
                    import subprocess as subprocess_mod

                    argv = [
                        sys.executable, "-c",
                        "import sys; print('MARK-CHILD-OUT'); "
                        "sys.stderr.write('MARK-CHILD-ERR\\n')",
                    ]
                    cli_mod._run_child(subprocess_mod.run, argv)
                finally:
                    guard.restore()
                os._exit(0)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"
        assert "MARK-CHILD-OUT" in out_path.read_text()
        assert "MARK-CHILD-ERR" in err_path.read_text()

    def test_no_kwargs_fallback(self):
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod

        guard = stdio_mod.start()
        try:
            def strict(argv, check=False):
                return "sentinel"

            with pytest.raises(TypeError):
                cli_mod._run_child(strict, ["sudo", "install"])
        finally:
            guard.restore()


class TestSeal:
    """S6: seal flushes the real streams, voids fds 1/2, and never
    touches a module-reachable stream."""

    def test_seal_flushes_and_redirects_without_touching_wrappers(
        self, tmp_path
    ):
        _needs_fork()
        _needs_proc_self_fd()
        from housesitter.core import stdio as stdio_mod

        marker_path = tmp_path / "seal-marker.txt"
        marker_path.write_bytes(b"")
        pid = os.fork()
        if pid == 0:  # child: exact fds, then straight to _exit
            try:
                for name in os.listdir("/proc/self/fd"):
                    fd = int(name)
                    if fd > 2:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                f = os.open(
                    str(marker_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                os.dup2(f, 1)
                os.dup2(f, 2)
                os.close(f)
                reader = os.open(str(marker_path), os.O_RDONLY)
                before = _open_fd_set()
                guard = stdio_mod.start()
                try:
                    # Buffered on purpose: only seal's flush may deliver it.
                    guard.stdout.write("MARK-SEAL-FLUSH\n")
                    old_out, old_err = sys.stdout, sys.stderr
                    wrap_out_no = sys.stdout.fileno()
                    wrap_err_no = sys.stderr.fileno()
                    real_out_no = guard.stdout.fileno()
                    guard.seal()
                except BaseException:
                    os._exit(16)
                s1, s2 = os.fstat(1), os.fstat(2)
                devnull = os.stat("/dev/null")
                after = _open_fd_set()
                try:
                    os.fstat(real_out_no)
                    real_closed = False
                except OSError:
                    real_closed = True
                try:
                    os.fstat(wrap_out_no)
                    os.fstat(wrap_err_no)
                    wrappers_open = True
                except OSError:
                    wrappers_open = False
                ok = (s1.st_ino, s1.st_dev) == (devnull.st_ino, devnull.st_dev)
                ok = ok and (s2.st_ino, s2.st_dev) == (
                    devnull.st_ino, devnull.st_dev)
                # The sys streams (module-reachable) are untouched and
                # still open; the core-owned reserved streams are closed.
                ok = ok and sys.stdout is old_out
                ok = ok and sys.stderr is old_err
                ok = ok and wrappers_open
                ok = ok and real_closed
                ok = ok and guard.stdout is None and guard.stderr is None
                # Only the two wrapper dups remain besides the reader.
                ok = ok and after - before == {
                    str(wrap_out_no), str(wrap_err_no),
                }
                os.lseek(reader, 0, os.SEEK_SET)
                data = os.read(reader, 100000)
                ok = ok and b"MARK-SEAL-FLUSH\n" in data
                ok = ok and stdio_mod.active() is None
                os._exit(0 if ok else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"


class TestRegistryImportTime:
    """Registry import-time output is caught because the guard starts first."""

    def test_registry_import_print_is_caught(self, tmp_path):
        proc = run_helper(tmp_path, "import_registry")
        assert proc.returncode == 0
        expected = len(b"STRAY-MARKER-import-time-leak\n")
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert "STRAY-MARKER-import-time-leak" not in proc.stdout.decode(
            "utf-8")
        assert "STRAY-MARKER-import-time-leak" not in proc.stderr.decode(
            "utf-8")


class TestCBuffered:
    """C-level buffered stdio never reaches the real streams."""

    def test_c_buffered_output_never_leaks(self, tmp_path):
        try:
            import ctypes

            ctypes.CDLL("libc.so.6")
        except OSError:
            pytest.skip("needs libc.so.6")
        proc = run_helper(tmp_path, "c_buffered")
        assert proc.returncode == 0
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert stdio_helper.MARK_C.encode() not in proc.stdout
        assert stdio_helper.MARK_C.encode() not in proc.stderr


class TestRunnerDoesNotGuard:
    """run_command never installs the guard, so conformance still detects."""

    def test_run_command_installs_no_guard(self, tmp_path):
        from housesitter.core import stdio as stdio_mod
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry
        from housesitter.core.runner import run_command

        seen = {}
        before_1, before_2 = _fd_identity(1), _fd_identity(2)

        def probe(self, ctx, params):
            seen["active"] = stdio_mod.active()
            seen["f1"] = _fd_identity(1)
            seen["f2"] = _fd_identity(2)
            return sdk.Result(data={"count": 1})

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "NoGuardModule",
            (sdk.Module,),
            {
                "id": "no_guard",
                "title": "No guard",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": probe,
            },
        )
        registry = Registry.from_classes([module_cls])
        config = Config(
            path=str(tmp_path / "config.json"),
            secrets_dir=str(tmp_path / "secrets"),
            identifiers="full",
            retention=500,
            modules={"no_guard": ModuleConfig(enabled=True, retention=500)},
        )
        outcome = run_command(
            "housesitter.no_guard.probe",
            {},
            registry=registry,
            env={"HOME": str(tmp_path)},
            config=config,
            log_stream=io.StringIO(),
        )
        assert outcome.exit_code == 0
        assert seen["active"] is None
        assert seen["f1"] == before_1
        assert seen["f2"] == before_2
        assert stdio_mod.active() is None


class TestStrayCountConsistency:
    """The envelope warning and the stderr log line report the same count."""

    def test_envelope_warning_matches_stderr_log_line(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry

        def probe(self, ctx, params):
            return sdk.Result(data={"count": 1})

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "ConsistentModule",
            (sdk.Module,),
            {
                "id": "consistent",
                "title": "Consistent",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": probe,
            },
        )
        registry = Registry.from_classes([module_cls])
        config = Config(
            path=str(tmp_path / "config.json"),
            secrets_dir=str(tmp_path / "secrets"),
            identifiers="full",
            retention=500,
            modules={"consistent": ModuleConfig(enabled=True, retention=500)},
        )

        class FakeGuard:
            def __init__(self):
                self.calls = 0
                self.stdout = None
                self.stderr = None

            def count(self):
                self.calls += 1
                return [5, 9][min(self.calls - 1, 1)]

            def seal(self):
                pass

            def restore(self):
                pass

        fake = FakeGuard()
        monkeypatch.setattr(stdio_mod, "start", lambda: fake)
        out, err = io.StringIO(), io.StringIO()
        code = cli_mod.main(
            ["invoke", "housesitter.consistent.probe", "--params", "{}"],
            env={"HOME": str(tmp_path)},
            stdin=io.BytesIO(b""),
            stdout=out,
            stderr=err,
            _exit=no_exit,
            registry=registry,
            config=config,
        )
        assert code == 0
        body = json.loads(out.getvalue())
        warn_count = int(
            re.search(r"wrote (\d+) bytes", body["warnings"][0]).group(1)
        )
        line, log_count = stray_line(err.getvalue())
        assert line is not None
        assert warn_count == log_count == 5
        # F7: the guard's count is sampled exactly once on success: the
        # runner samples it right after the handler returns, and the CLI
        # reuses that value instead of sampling again.
        assert fake.calls == 1


class TestGILHoldingWriters:
    """D1: writers that hold the GIL must never hang the process."""

    def test_faulthandler_dump_finishes_with_valid_envelope(
        self, tmp_path
    ):
        out, err, code = run_helper_limited(tmp_path, "fault_dump", 15)
        assert code == 0
        # One valid envelope: any stray bytes on real stdout would
        # break this parse.
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        assert stdio_helper.MARK_FAULT.encode() not in out
        assert stdio_helper.MARK_FAULT.encode() not in err
        line, count = stray_line(err.decode("utf-8"))
        assert line is not None and count > 0

    def test_c_printf_without_flush_finishes(self, tmp_path):
        try:
            import ctypes

            ctypes.PyDLL(None)
        except Exception:
            pytest.skip("needs ctypes PyDLL(None)")
        out, err, code = run_helper_limited(tmp_path, "c_printf_big", 15)
        assert code == 0
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        assert b"x" * 5000 not in out
        assert b"x" * 5000 not in err


class TestBlockingModuleStreams:
    """D2: sampling the count never flushes module streams."""

    def test_blocking_stdout_flush_still_times_out(self, tmp_path):
        out, err, code = run_helper_limited(tmp_path, "flush_block", 15)
        assert code == 1
        body = json.loads(out.decode("utf-8"))
        assert body == {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "the command did not finish within its time limit",
            },
        }
        lines = err.decode("utf-8").splitlines()
        assert lines[-1].startswith("housesitter: stdio_probe: timeout: ")

    def test_blocked_buffer_lock_still_times_out(self, tmp_path):
        out, err, code = run_helper_limited(tmp_path, "blocked_buffer", 15)
        assert code == 1
        body = json.loads(out.decode("utf-8"))
        assert body == {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "the command did not finish within its time limit",
            },
        }
        lines = err.decode("utf-8").splitlines()
        assert lines[-1].startswith("housesitter: stdio_probe: timeout: ")


class TestManyOpenFds:
    """D3: the sink works with more than FD_SETSIZE fds open."""

    def test_many_open_fds_exact_count(self, tmp_path):
        out, err, code = run_helper_limited(tmp_path, "highfd", 25)
        if code == 3 or b"HIGHFD-SKIP" in out:
            pytest.skip("could not open 1100 fds")
        assert code == 0
        expected = len(b"x" * 90000 + b"\n") + len(b"y" * 90000 + b"\n")
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        assert b"x" * 5000 not in out
        assert b"y" * 5000 not in err
        line, count = stray_line(err.decode("utf-8"))
        assert line is not None and count == expected


class TestRollbackSingleOwner:
    """D4: a failed start closes each fd exactly once, even on reuse."""

    def test_failed_start_never_closes_reused_fds(self, monkeypatch):
        from housesitter.core import stdio

        _needs_proc_self_fd()
        real_close = os.close
        replacements = []

        def close(fd):
            real_close(fd)
            # Force the interleaving of a thread opening a file in the
            # gap: plant a live replacement on the freed number. A
            # second close of the same number would kill it.
            try:
                fresh = os.open("/dev/null", os.O_WRONLY)
            except OSError:
                return
            if fresh != fd:
                try:
                    os.dup2(fresh, fd)
                finally:
                    try:
                        real_close(fresh)
                    except OSError:
                        pass
            replacements.append(fd)

        def boom(self):
            raise RuntimeError("injected trim-thread failure")

        monkeypatch.setattr(os, "close", close)
        monkeypatch.setattr(threading.Thread, "start", boom)
        before_fds = _open_fd_set()
        before_out, before_err = sys.stdout, sys.stderr
        before_1, before_2 = _fd_identity(1), _fd_identity(2)
        assert stdio.active() is None
        try:
            with pytest.raises(RuntimeError):
                stdio.start()
            assert stdio.active() is None
            assert sys.stdout is before_out
            assert sys.stderr is before_err
            assert _fd_identity(1) == before_1
            assert _fd_identity(2) == before_2
            for fd in replacements:
                try:
                    os.fstat(fd)
                except OSError:
                    pytest.fail(
                        f"replacement fd {fd} was closed twice "
                        "during rollback"
                    )
        finally:
            for fd in replacements:
                try:
                    real_close(fd)
                except OSError:
                    pass
        assert _open_fd_set() == before_fds


class TestNestedDiscard:
    """G3: a nested refusal writes nothing to the outer real streams."""

    def test_nested_invoke_writes_nowhere(self, tmp_path):
        _needs_fork()
        from housesitter import sdk
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry

        ran_path = tmp_path / "nested-ran.txt"
        out_path = tmp_path / "nested-out.txt"
        err_path = tmp_path / "nested-err.txt"
        out_path.write_bytes(b"")
        err_path.write_bytes(b"")
        pid = os.fork()
        if pid == 0:  # child: real fds to files, then straight to _exit
            try:
                f1 = os.open(
                    str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                f2 = os.open(
                    str(err_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                os.dup2(f1, 1)
                os.dup2(f2, 2)
                os.close(f1)
                os.close(f2)

                def probe(self, ctx, params):
                    with open(str(ran_path), "w"):
                        pass
                    return sdk.Result(data={"count": 1})

                command = sdk.Command(
                    name="probe",
                    handler="probe",
                    description="Read a probe value from the test sensor.",
                    fields=(sdk.Field("data.count", "integer"),),
                )
                module_cls = type(
                    "NestedProbeModule",
                    (sdk.Module,),
                    {
                        "id": "nested_probe",
                        "title": "Nested probe",
                        "version": "0.1.0",
                        "config": {},
                        "http_allow": (),
                        "commands": (command,),
                        "probe": probe,
                    },
                )
                registry = Registry.from_classes([module_cls])
                config = Config(
                    path=str(tmp_path / "config.json"),
                    secrets_dir=str(tmp_path / "secrets"),
                    identifiers="full",
                    retention=500,
                    modules={
                        "nested_probe": ModuleConfig(
                            enabled=True, retention=500
                        )
                    },
                )
                guard = stdio_mod.start()
                try:
                    # No injected streams: production shape, nested.
                    code = cli_mod.main(
                        ["invoke", "housesitter.nested_probe.probe",
                         "--params", "{}"],
                        env={"HOME": str(tmp_path)},
                        registry=registry,
                        config=config,
                    )
                    # The refusal path flushes what it writes, so the
                    # files themselves (not fds 1/2, which are the
                    # guard's sink here) show any leak exactly.
                    out_size = os.stat(str(out_path)).st_size
                    err_size = os.stat(str(err_path)).st_size
                finally:
                    guard.restore()
                ok = (
                    code == 1
                    and out_size == 0
                    and err_size == 0
                    and not ran_path.exists()
                )
                os._exit(0 if ok else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"


    def test_nested_call_arms_no_watchdog(self, tmp_path):
        # A nested production-shape cli.main() must not start a delivery
        # watchdog: it would kill the outer process (still running the
        # outer handler) once it fired. Refusals discard their output and
        # continuing subcommands exit normally, so neither needs one.
        _needs_fork()
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod
        from housesitter.core.registry import Registry

        pid = os.fork()
        if pid == 0:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                os.close(devnull)
                cli_mod._SEAL_WATCHDOG_S = 0.3
                registry = Registry.from_classes([])
                guard = stdio_mod.start()
                try:
                    codes = [
                        cli_mod.main(
                            argv,
                            env={"HOME": str(tmp_path)},
                            registry=registry,
                        )
                        for argv in (["describe"], ["list"])
                    ]
                    # Outlive any armed timer: it would os._exit(1).
                    time.sleep(1.5)
                finally:
                    guard.restore()
                os._exit(0 if codes == [1, 0] else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0, (
            f"child exited {os.WEXITSTATUS(status)}"
        )


class TestTrimMemory:
    """The trim thread bounds memory while the count stays exact."""

    def test_trim_keeps_memory_small_count_exact(self):
        from housesitter.core import stdio

        guard = stdio.start()
        try:
            chunk = b"m" * 65536
            want = 8 * 1024 * 1024
            written = 0
            while written < want:
                written += os.write(1, chunk)
            assert guard.count() == want
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if os.fstat(guard._sink_fd).st_size == 0:
                    break
                time.sleep(0.05)
            st = os.fstat(guard._sink_fd)
            assert st.st_size == 0
            assert st.st_blocks * 512 < 1024 * 1024
            assert guard.count() == want
        finally:
            guard.restore()


class TestRestoreFourStreams:
    """restore() puts back stdout, stderr, __stdout__, __stderr__."""

    def test_restore_returns_all_four_streams(self):
        from housesitter.core import stdio

        before = (
            sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__,
        )
        guard = stdio.start()
        try:
            assert sys.stdout is not before[0]
            assert sys.stderr is not before[1]
            if before[2] is not None:
                assert sys.__stdout__ is not before[2]
            if before[3] is not None:
                assert sys.__stderr__ is not before[3]
        finally:
            guard.restore()
        assert (
            sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__,
        ) == before

    def test_memfd_missing_refuses_cleanly(self, monkeypatch):
        from housesitter.core import stdio

        _needs_proc_self_fd()
        monkeypatch.delattr(os, "memfd_create", raising=False)
        before_fds = _open_fd_set()
        before_out, before_err = sys.stdout, sys.stderr
        before_1, before_2 = _fd_identity(1), _fd_identity(2)
        assert stdio.active() is None
        with pytest.raises(OSError):
            stdio.start()
        assert stdio.active() is None
        assert sys.stdout is before_out
        assert sys.stderr is before_err
        assert _fd_identity(1) == before_1
        assert _fd_identity(2) == before_2
        assert _open_fd_set() == before_fds


class TestSealNeverTouchesModuleStreams:
    """R1 (blocker): seal() never invokes module-reachable streams.

    The handler mutates the EXISTING sink wrapper's flush to block; a
    seal that closes the wrapper would hang instead of exiting. Each
    test fails by tight subprocess timeout when the process hangs.
    """

    def test_evil_flush_timeout_still_exits(self, tmp_path):
        out, err, code = run_helper_limited(
            tmp_path, "seal_flush_timeout", 10
        )
        assert code == 1
        body = json.loads(out.decode("utf-8"))
        assert body == {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "the command did not finish within its time limit",
            },
        }
        lines = err.decode("utf-8").splitlines()
        assert lines[-1].startswith("housesitter: stdio_probe: timeout: ")

    def test_evil_flush_success_still_exits(self, tmp_path):
        out, err, code = run_helper_limited(
            tmp_path, "seal_flush_success", 10
        )
        assert code == 0
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True

    def test_zombie_thread_does_not_keep_process_alive(self, tmp_path):
        start = time.monotonic()
        out, err, code = run_helper_limited(tmp_path, "zombie_thread", 10)
        elapsed = time.monotonic() - start
        assert code == 0
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        # The handler thread sleeps 30 s; waiting for it would take 30 s.
        assert elapsed < 10

    def test_version_and_argparse_errors_stay_normal(self):
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        driver = "from housesitter.core.cli import main; main({argv})"
        proc = subprocess.run(
            [sys.executable, "-c", driver.format(argv="['--version']")],
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0
        assert proc.stdout.decode("utf-8").startswith("housesitter ")
        proc = subprocess.run(
            [sys.executable, "-c", driver.format(argv="['invoke']")],
            capture_output=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 2
        assert "usage:" in proc.stderr.decode("utf-8")


class TestConcurrentWritersExact:
    """R2: concurrent writers through one shared description count exactly."""

    def test_four_threads_os_write_exact_with_trimming(self):
        from housesitter.core import stdio

        # Pace the trimmer so several trims must land mid-run, count every
        # trim call, and pace the writers so they are still running when
        # those trims happen. The count must stay exact regardless.
        # Saved and restored manually (not via monkeypatch) so the test
        # stays directly callable outside pytest too.
        old_interval = stdio._TRIM_INTERVAL_S
        real_ftruncate = os.ftruncate
        trims = []
        trim_lock = threading.Lock()

        def counting_ftruncate(fd, length):
            with trim_lock:
                trims.append(1)
            return real_ftruncate(fd, length)

        stdio._TRIM_INTERVAL_S = 0.01
        os.ftruncate = counting_ftruncate  # type: ignore[assignment]
        try:
            guard = stdio.start()
            try:
                per_thread = 200
                chunk = b"x" * 4096
                totals = []
                lock = threading.Lock()

                def _write():
                    total = 0
                    for index in range(per_thread):
                        total += os.write(1, chunk)
                        if index % 10 == 0:
                            # Yield so the 10 ms trimmer runs while
                            # writers are still active; ~20 yields per
                            # thread keeps the run well under a few
                            # seconds.
                            time.sleep(0.01)
                    with lock:
                        totals.append(total)

                threads = [
                    threading.Thread(target=_write) for _ in range(4)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                assert sum(totals) == 4 * per_thread * len(chunk)
                assert guard.count() == sum(totals)
                with trim_lock:
                    seen = len(trims)
                assert seen >= 1, (
                    "writers finished with no trim observed mid-run: "
                    "this test must cover concurrent trimming"
                )
            finally:
                guard.restore()
        finally:
            stdio._TRIM_INTERVAL_S = old_interval
            os.ftruncate = real_ftruncate

    def test_threaded_cli_handler_counts_exactly(self, tmp_path):
        out, err, code = run_helper_limited(tmp_path, "threaded_exact", 20)
        assert code == 0
        expected = 4 * 512 * 1025
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        assert body["warnings"][0] == WARNING_TEMPLATE.format(n=expected)
        line, count = stray_line(err.decode("utf-8"))
        assert line is not None and count == expected


class TestOAppend:
    """R3: O_APPEND on the sink must not rewind the count at trim time."""

    def test_o_append_write_trim_write_counts_exact(self):
        fcntl = pytest.importorskip("fcntl")
        from housesitter.core import stdio

        guard = stdio.start()
        try:
            flags = fcntl.fcntl(1, fcntl.F_GETFL)
            fcntl.fcntl(1, fcntl.F_SETFL, flags | os.O_APPEND)
            try:
                os.write(1, b"x" * 100)
                deadline = time.monotonic() + 5
                while os.fstat(guard._sink_fd).st_size != 0:
                    if time.monotonic() > deadline:
                        pytest.fail("the trim thread never ran")
                    time.sleep(0.01)
                os.write(1, b"y" * 100)
                assert guard.count() == 200
            finally:
                try:
                    fcntl.fcntl(1, fcntl.F_SETFL, flags)
                except OSError:
                    pass
        finally:
            guard.restore()


class TestRestoreNoneStreams:
    """R4: restore()/rollback put back saved originals, even None."""

    @pytest.mark.parametrize(
        "name", ["stdout", "stderr", "__stdout__", "__stderr__"]
    )
    def test_restore_puts_back_none(self, name):
        from housesitter.core import stdio

        before = getattr(sys, name)
        setattr(sys, name, None)
        try:
            guard = stdio.start()
            try:
                pass
            finally:
                guard.restore()
            assert getattr(sys, name) is None
        finally:
            setattr(sys, name, before)


class TestLateWorkerLogging:
    """R5: a timed-out worker's late logs never reach real stderr."""

    def test_timeout_worker_logs_stay_off_real_stderr(
        self, tmp_path, capsys
    ):
        import logging

        from housesitter.core import cli as cli_mod
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry

        # Production has no root handlers, so a late record would use
        # logging.lastResort straight to real stderr. pytest installs
        # root handlers that would mask that; strip them to emulate it.
        root = logging.getLogger()
        saved_root = list(root.handlers)
        for handler in saved_root:
            root.removeHandler(handler)
        marker = "LATE-LOG-MARKER-r5"
        finished = threading.Event()

        def probe(self, ctx, params):
            time.sleep(3.2)
            logging.getLogger("housesitter").warning(marker + "-parent")
            logging.getLogger("housesitter.late_log").error(
                marker + "-child"
            )
            finished.set()
            return sdk.Result(data={"count": 1})

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
            timeout_ms=5000,
        )
        module_cls = type(
            "LateLogModule",
            (sdk.Module,),
            {
                "id": "late_log",
                "title": "Late log",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": probe,
            },
        )
        try:
            registry = Registry.from_classes([module_cls])
            config = Config(
                path=str(tmp_path / "config.json"),
                secrets_dir=str(tmp_path / "secrets"),
                identifiers="full",
                retention=500,
                modules={"late_log": ModuleConfig(enabled=True, retention=500)},
            )
            out, err = io.StringIO(), io.StringIO()
            codes = []
            code = cli_mod.main(
                ["invoke", "housesitter.late_log.probe", "--params", "{}"],
                env={"HOME": str(tmp_path)},
                stdin=io.BytesIO(b""),
                stdout=out,
                stderr=err,
                _exit=codes.append,
                registry=registry,
                config=config,
            )
            assert code == 1
            assert codes == [1]
            assert finished.wait(15)
            lines = err.getvalue().splitlines()
            assert lines[-1] == (
                "housesitter: late_log: timeout: "
                "the command did not finish within its time limit"
            )
            assert marker not in err.getvalue()
            captured = capsys.readouterr()
            assert marker not in captured.err
            assert marker not in captured.out
        finally:
            for handler in saved_root:
                root.addHandler(handler)


class TestUnknownSubcommandRouting:
    """R6: post-guard parser errors go to the real stderr, exit 2."""

    def test_unknown_subcommand_uses_err_not_sink(self):
        from housesitter.core import cli as cli_mod

        parser = cli_mod.build_parser()
        args = argparse.Namespace(subcommand="no-such-command")
        out, err = io.StringIO(), io.StringIO()
        code = cli_mod._main_guarded(
            args, parser, {}, None, out, err, no_exit,
            object(), None, None, None, None, False,
        )
        assert code == 2
        assert out.getvalue() == ""
        assert "no-such-command" in err.getvalue()


class TestUnguardedRunChild:
    """R7: with no guard, the runner is called with no stream kwargs."""

    def test_unguarded_call_has_no_stdout_stderr_kwargs(self):
        from housesitter.core import cli as cli_mod
        from housesitter.core import stdio as stdio_mod

        assert stdio_mod.active() is None
        seen = {}

        def strict(argv, check=False, **kwargs):
            seen["argv"] = argv
            seen["check"] = check
            seen["kwargs"] = kwargs
            return "sentinel"

        assert cli_mod._run_child(strict, ["sudo", "install"]) == "sentinel"
        assert seen["argv"] == ["sudo", "install"]
        assert seen["check"] is True
        assert seen["kwargs"] == {}


class TestClaimFdNoLeak:
    """R8: if F_DUPFD raises after os.dup, the raw fd is closed."""

    def test_fcntl_failure_closes_raw_fd(self):
        _needs_fork()
        from housesitter.core import stdio

        if not stdio._HAVE_FCNTL:
            pytest.skip("needs fcntl")
        pid = os.fork()
        if pid == 0:  # child: controlled fds, then straight to _exit
            try:
                try:
                    os.close(0)
                except OSError:
                    pass
                # Freshly created (stdin was closed above); _claim_fd
                # must take the F_DUPFD path for it.
                fresh = os.open("/dev/null", os.O_RDONLY)
                if fresh != 0:
                    os._exit(17)
                real_fcntl = stdio.fcntl.fcntl

                def boom(fd, *args):
                    raise OSError("injected F_DUPFD failure")

                stdio.fcntl.fcntl = boom
                try:
                    try:
                        stdio._claim_fd(fresh)
                        ok = False
                    except OSError:
                        try:
                            os.fstat(fresh)
                            ok = False  # leaked: still open
                        except OSError:
                            ok = True
                finally:
                    stdio.fcntl.fcntl = real_fcntl
                os._exit(0 if ok else 17)
            except BaseException:
                os._exit(16)
        _, status = os.waitpid(pid, 0)
        assert status == 0, f"child failed with status {status}"


class TestTrimPrivateFd:
    """R9: the trim thread owns a private dup; it only closes that one."""

    def test_trim_fd_is_private_and_recycled_numbers_are_safe(self, tmp_path):
        from housesitter.core import stdio

        _needs_proc_self_fd()
        guard = stdio.start()
        decoy = None
        decoy_path = tmp_path / "decoy.bin"
        try:
            trim_fd = getattr(guard, "_trim_fd", None)
            assert trim_fd is not None and trim_fd >= 3
            assert trim_fd != guard._sink_fd
            sink_stat = os.fstat(guard._sink_fd)
            trim_stat = os.fstat(trim_fd)
            assert (trim_stat.st_ino, trim_stat.st_dev) == (
                sink_stat.st_ino, sink_stat.st_dev,
            )
            # Free the guard's sink number and plant a decoy file on it:
            # a trim that outlives the bounded join must never
            # ftruncate this recycled number.
            os.close(guard._sink_fd)
            decoy = os.open(str(decoy_path), os.O_RDWR | os.O_CREAT, 0o600)
            assert decoy == guard._sink_fd
            # Keep restore() from closing our decoy: the number is ours.
            guard._sink_fd = -1
            os.write(decoy, b"decoy")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                time.sleep(0.15)
                os.lseek(decoy, 0, os.SEEK_SET)
                if os.fstat(decoy).st_size != 5:
                    pytest.fail("the trim thread hit a recycled fd number")
            os.lseek(decoy, 0, os.SEEK_SET)
            assert os.read(decoy, 16) == b"decoy"
            os.close(decoy)
            decoy = None
        finally:
            try:
                guard.restore()
            finally:
                if decoy is not None:
                    try:
                        os.close(decoy)
                    except OSError:
                        pass
        if decoy_path.exists():
            assert decoy_path.stat().st_size == 5


class TestWrapFailureRollback:
    """R11: open/TextIOWrapper failures keep single fd ownership."""

    def _check_clean_rollback(self):
        from housesitter.core import stdio

        assert stdio.active() is None
        assert _fd_identity(1) == self._before_1
        assert _fd_identity(2) == self._before_2
        assert _open_fd_set() == self._before_fds
        assert sys.stdout is self._before_out
        assert sys.stderr is self._before_err
        assert (sys.__stdout__, sys.__stderr__) == self._before_dunder

    def _snapshot(self):
        self._before_fds = _open_fd_set()
        self._before_out, self._before_err = sys.stdout, sys.stderr
        self._before_dunder = (sys.__stdout__, sys.__stderr__)
        self._before_1, self._before_2 = _fd_identity(1), _fd_identity(2)

    def test_textiowrapper_failure_rolls_back_cleanly(self, monkeypatch):
        import io as io_mod

        from housesitter.core import stdio

        _needs_proc_self_fd()
        self._snapshot()

        def boom(*args, **kwargs):
            raise OSError("injected wrapper failure")

        monkeypatch.setattr(io_mod, "TextIOWrapper", boom)
        with pytest.raises(OSError):
            stdio.start()
        self._check_clean_rollback()

    @pytest.mark.parametrize("fail_on", [1, 3])
    def test_open_failure_rolls_back_cleanly(self, monkeypatch, fail_on):
        import builtins

        from housesitter.core import stdio

        _needs_proc_self_fd()
        self._snapshot()
        real_open = builtins.open
        calls = {"count": 0}

        def counting_open(*args, **kwargs):
            # Calls 1-2 wrap the reserved real streams, 3-4 the sink
            # wrappers: fail one helper's open and check its raw fd is
            # closed here, never as a raw fd in the abort path.
            calls["count"] += 1
            if calls["count"] == fail_on:
                raise OSError("injected open failure")
            return real_open(*args, **kwargs)

        monkeypatch.setattr(builtins, "open", counting_open)
        with pytest.raises(OSError):
            stdio.start()
        self._check_clean_rollback()

    def test_proc_reopen_failure_rolls_back_cleanly(self, monkeypatch):
        from housesitter.core import stdio

        _needs_proc_self_fd()
        self._snapshot()
        real_open = os.open

        def boom(path, *args):
            if (
                isinstance(path, str)
                and path.startswith("/proc/self/fd/")
            ):
                raise OSError("injected reopen failure")
            return real_open(path, *args)

        monkeypatch.setattr(os, "open", boom)
        try:
            guard = stdio.start()
        except OSError:
            self._check_clean_rollback()
        else:
            guard.restore()
            pytest.fail("start() has no /proc reopen to fail")


class TestTrimPeakBound:
    """R12: peak sink size stays bounded during a sustained writer."""

    def test_peak_sink_size_stays_bounded(self):
        from housesitter.core import stdio

        guard = stdio.start()
        try:
            # Allocated blocks, not st_size: the shared offset only moves
            # forward, so st_size is the sparse extent even right after a
            # trim. Blocks are the RAM the sink actually holds.
            peak = 0
            chunk = b"p" * 4096
            total = 2000
            for index in range(total):
                os.write(1, chunk)
                # Let the GIL go so the trim thread runs between writes.
                time.sleep(0.0005)
                if index % 10 == 0:
                    allocated = os.fstat(guard._sink_fd).st_blocks * 512
                    peak = max(peak, allocated)
            assert guard.count() == total * len(chunk)
            assert peak < 4 * 1024 * 1024
        finally:
            guard.restore()


# The fixed seal-mode diagnostics for an unexpected failure: no exception
# text (it may be untrusted or secret), always the same bytes.
FIXED_LINE = (
    "housesitter: housesitter: upstream_unexpected: "
    "the command failed unexpectedly"
)
FIXED_ENVELOPE = {
    "ok": False,
    "error": {
        "code": "upstream_unexpected",
        "message": "the command failed unexpectedly",
    },
}


class TestSealUnexpectedEscape:
    """E1 (blocker): ANY BaseException in seal mode hard-exits 1.

    The old path sealed and re-raised into normal interpreter shutdown:
    shutdown could hang on module-reachable streams, destructors, module
    atexit hooks, or non-daemon threads; the traceback went to the voided
    sink so the real stderr got nothing; and SystemExit(0) exited 0 with
    no JSON. Each test fails by tight subprocess timeout when the
    process hangs instead of hard-exiting.
    """

    def test_import_systemexit_zero_exits_1_with_envelope(self, tmp_path):
        proc = run_helper(tmp_path, "import_exit0")
        assert proc.returncode == 1
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body == FIXED_ENVELOPE
        err_text = proc.stderr.decode("utf-8")
        assert "Traceback" not in err_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_body_exception_exits_1_without_leak(self, tmp_path):
        proc = run_helper(tmp_path, "body_raise")
        assert proc.returncode == 1
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body == FIXED_ENVELOPE
        err_text = proc.stderr.decode("utf-8")
        out_text = proc.stdout.decode("utf-8")
        assert "Traceback" not in err_text
        assert "simulated unexpected failure" not in err_text
        assert "simulated unexpected failure" not in out_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_broken_pipe_envelope_write_exits_1(self, tmp_path):
        # Stdout is a pipe whose read end is closed: the envelope write
        # raises BrokenPipeError. Stderr stays a writable captured pipe,
        # so the fixed error line must arrive there as the last line.
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        home = tmp_path / "home-broken-pipe"
        home.mkdir(exist_ok=True)
        proc = subprocess.Popen(
            [sys.executable, str(HELPER), str(home), "quiet"],
            stdout=write_fd,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(write_fd)
        try:
            _, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            _, err = proc.communicate()
            pytest.fail("broken-pipe invoke did not finish within 60s")
        assert proc.returncode == 1
        err_text = err.decode("utf-8")
        assert "Traceback" not in err_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_atexit_and_thread_do_not_hang_failure_exit(self, tmp_path):
        start = time.monotonic()
        out, err, code = run_helper_limited(
            tmp_path, "body_raise_atexit_thread", 15
        )
        elapsed = time.monotonic() - start
        assert code == 1
        # The hook sleeps 30 s and the thread sleeps 30 s: waiting for
        # either would overrun this bound several times over.
        assert elapsed < 15
        body = json.loads(out.decode("utf-8"))
        assert body == FIXED_ENVELOPE
        err_text = err.decode("utf-8")
        assert "Traceback" not in err_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE


class TestSealFlushDelivery:
    """E2: exit 0 only if the real streams flushed completely."""

    def _run_full_stdout(self, tmp_path, cmd):
        home = tmp_path / f"home-full-{cmd}"
        home.mkdir(exist_ok=True)
        full = os.open("/dev/full", os.O_WRONLY)
        try:
            proc = subprocess.Popen(
                [sys.executable, str(HELPER), str(home), "quiet", cmd],
                stdout=full,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            os.close(full)
        try:
            _, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            _, err = proc.communicate()
            pytest.fail(f"{cmd} to /dev/full did not finish within 60s")
        return proc.returncode, err

    def test_invoke_full_stdout_exits_1(self, tmp_path):
        code, err = self._run_full_stdout(tmp_path, "invoke")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_list_full_stdout_exits_1(self, tmp_path):
        code, err = self._run_full_stdout(tmp_path, "list")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_sigint_during_final_flush_never_exits_0_empty(self, tmp_path):
        # SIGINT lands right after the trim join, i.e. during the final
        # flush of the buffered list output: the output is lost, so the
        # exit must be nonzero, never a quiet 0 with empty stdout.
        out, err, code = run_helper_limited(
            tmp_path, "seal_sigint", 15, cmd="list"
        )
        assert code == 1
        assert not (code == 0 and out == b"")
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE


class TestProductionSubcommandMatrix:
    """E3: exact exit code and exact stdout/stderr bytes under sealing."""

    def _registry(self):
        from housesitter.core.registry import Registry

        return Registry.from_classes([stdio_helper.make_module("quiet")])

    def test_list_exact_bytes(self, tmp_path):
        proc = run_helper(tmp_path, "quiet", cmd="list")
        assert proc.returncode == 0
        assert proc.stdout.decode("utf-8") == (
            "housesitter.describe\tcore\t"
            "List installed Housesitter modules, their commands and "
            "parameters, and each module's SKILL.md guide\n"
            "housesitter.doctor\tcore\t"
            "Check Housesitter's own setup: config file, secret files, "
            "data directory, installed drop-in files, and the account's "
            "sudo group\n"
            "housesitter.stdio_probe.probe\tenabled\t"
            "Read a probe value from the test sensor\n"
        )
        assert proc.stderr == b""

    def test_list_json_exact_bytes(self, tmp_path):
        proc = run_helper(tmp_path, "quiet", cmd="list-json")
        assert proc.returncode == 0
        expected = json.dumps(
            [
                {
                    "name": "housesitter.describe",
                    "status": "core",
                    "description": (
                        "List installed Housesitter modules, their "
                        "commands and parameters, and each module's "
                        "SKILL.md guide"
                    ),
                },
                {
                    "name": "housesitter.doctor",
                    "status": "core",
                    "description": (
                        "Check Housesitter's own setup: config file, "
                        "secret files, data directory, installed "
                        "drop-in files, and the account's sudo group"
                    ),
                },
                {
                    "name": "housesitter.stdio_probe.probe",
                    "status": "enabled",
                    "description": (
                        "Read a probe value from the test sensor"
                    ),
                },
            ],
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ) + "\n"
        assert proc.stdout.decode("utf-8") == expected
        assert proc.stderr == b""

    def test_config_check_exact_bytes(self, tmp_path):
        proc = run_helper(tmp_path, "quiet", cmd="config-check")
        assert proc.returncode == 0
        assert proc.stdout.decode("utf-8") == (
            "config ok: ~/config.json\nstdio_probe: enabled\n"
        )
        assert proc.stderr == b""

    def test_dropins_print_exact_bytes(self, tmp_path):
        from housesitter.core import dropins as dropins_mod

        proc = run_helper(tmp_path, "quiet", cmd="dropins-print")
        assert proc.returncode == 0
        expected = json.dumps(
            dropins_mod.generate(self._registry(), "/usr/bin/housesitter"),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        ) + "\n"
        assert proc.stdout.decode("utf-8") == expected
        assert proc.stderr == b""

    def test_dropins_out_exact_bytes(self, tmp_path):
        from housesitter.core import dropins as dropins_mod

        proc = run_helper(tmp_path, "quiet", cmd="dropins-out")
        assert proc.returncode == 0
        files = dropins_mod.generate(
            self._registry(), "/usr/bin/housesitter"
        )
        home = tmp_path / "home-quiet-dropins-out"
        expected = "".join(name + "\n" for name in sorted(files))
        assert proc.stdout.decode("utf-8") == expected
        assert proc.stderr == b""
        for name in sorted(files):
            assert (home / "dropins" / name).is_file()

    def test_dry_run_exact_bytes(self, tmp_path):
        from housesitter.core import dropins as dropins_mod

        proc = run_helper(tmp_path, "quiet", cmd="dry-run")
        assert proc.returncode == 0
        files = dropins_mod.generate(
            self._registry(), "/usr/bin/housesitter"
        )
        home = tmp_path / "home-quiet-dry-run"
        dest = os.path.join(str(home), "dest")
        expected = "".join(
            f"would install {name} -> {dest}/{name}\n"
            for name in sorted(files)
        )
        expected += (
            "restart with: sudo systemctl restart musegadget\n"
            "warning: restarting musegadget stops any command "
            "Muse is running right now\n"
        )
        assert proc.stdout.decode("utf-8") == expected
        assert proc.stderr == b""


def _json_objects(data: bytes):
    """One entry per stdout line: the object, or None for a partial."""
    objects = []
    for line in data.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except ValueError:
            objects.append(None)
    return objects


def _set_pipe_capacity(read_fd, write_fd, size):
    """Shrink a pipe to ``size`` bytes so tests need no reader."""
    fcntl = pytest.importorskip("fcntl")
    # F_SETPIPE_SZ is 1031 on Linux; Python only names it from 3.10.
    try:
        fcntl.fcntl(write_fd, getattr(fcntl, "F_SETPIPE_SZ", 1031), size)
    except OSError:
        pytest.skip("cannot resize a pipe here")


def _popen_helper(home, scenario, cmd="invoke", stdout=None, stderr=None):
    home.mkdir(exist_ok=True)
    return subprocess.Popen(
        [sys.executable, str(HELPER), str(home), scenario, cmd],
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=stderr if stderr is not None else subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def _reap(proc, timeout, what):
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        proc.communicate()
        pytest.fail(f"{what} did not finish within {timeout}s")


class TestNoSecondEnvelope:
    """F1 (blocker): once output started, never a replacement envelope.

    A failed envelope flush followed by a later exception, and an
    exception raised partway through the envelope write, must leave at
    most the one original (possibly partial) envelope on stdout — never
    a second JSON object — with exit 1 and the fixed line last on
    stderr when stderr is usable.
    """

    def test_flush_fail_then_exception_keeps_single_envelope(
        self, tmp_path
    ):
        proc = run_helper(tmp_path, "flushfail_then_raise")
        assert proc.returncode == 1
        objects = _json_objects(proc.stdout)
        assert len(objects) == 1
        assert objects[0] is not None and objects[0].get("ok") is True
        assert b"upstream_unexpected" not in proc.stdout
        lines = proc.stderr.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_prefix_then_raise_never_appends_second_object(
        self, tmp_path
    ):
        proc = run_helper(tmp_path, "partial_prefix_raise")
        assert proc.returncode == 1
        assert len(proc.stdout) == 100
        assert proc.stdout.startswith(b'{"ok":true')
        assert _json_objects(proc.stdout) == [None]
        assert b"upstream_unexpected" not in proc.stdout
        lines = proc.stderr.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE


class TestSealSigintDiesPromptly:
    """F2 (blocker): SIGINT in seal mode kills promptly, never hangs.

    The helper sends SIGINT to itself at a hook point just before the
    hard exit, and at a point during final delivery, with a sleeping
    module atexit hook and a non-daemon thread present. Seal mode must
    end promptly with a nonzero status (signal death here), never exit
    0, never hang in interpreter shutdown.
    """

    @staticmethod
    def _run_sigint(tmp_path, scenario, timeout=10):
        home = tmp_path / f"home-{scenario}"
        home.mkdir(exist_ok=True)
        proc = _popen_helper(home, scenario)
        start = time.monotonic()
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            proc.communicate()
            pytest.fail(
                f"{scenario} hung past {timeout}s instead of dying by SIGINT"
            )
        return time.monotonic() - start, out, err, proc.returncode

    def test_sigint_just_before_hard_exit(self, tmp_path):
        elapsed, out, err, code = self._run_sigint(
            tmp_path, "sigint_before_exit"
        )
        assert code == -signal.SIGINT
        assert elapsed < 10
        assert len(_json_objects(out)) == 1

    def test_sigint_during_final_delivery(self, tmp_path):
        elapsed, out, err, code = self._run_sigint(
            tmp_path, "sigint_during_delivery"
        )
        assert code == -signal.SIGINT
        assert elapsed < 10
        assert len(_json_objects(out)) == 1


class TestFinalDeliveryWatchdog:
    """F3 (should-fix): blocked final delivery exits 1 within the bound.

    A daemon watchdog hard-exits 1 after the module constant when the
    reader of the real stdout or stderr stalls. Each test fails by
    tight subprocess timeout when the process hangs instead.
    """

    def test_stdout_pipe_nobody_reads_exits_1(self, tmp_path):
        read_fd, write_fd = os.pipe()
        try:
            _set_pipe_capacity(read_fd, write_fd, 4096)
            home = tmp_path / "home-big-envelope"
            proc = _popen_helper(
                home, "big_envelope", stdout=write_fd,
            )
            os.close(write_fd)
            write_fd = -1
            start = time.monotonic()
            _, err = _reap(proc, 15, "blocked stdout delivery")
            elapsed = time.monotonic() - start
            assert proc.returncode == 1
            assert elapsed < 15
            prefix = os.read(read_fd, 65536)
            assert 0 < len(prefix) <= 4096
        finally:
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
            try:
                os.close(read_fd)
            except OSError:
                pass

    def test_stderr_pipe_full_exits_1(self, tmp_path):
        read_fd, write_fd = os.pipe()
        try:
            _set_pipe_capacity(read_fd, write_fd, 4096)
            os.write(write_fd, b"x" * 4096)
            home = tmp_path / "home-print-leak-full-err"
            proc = _popen_helper(
                home, "print_leak", stdout=subprocess.PIPE,
                stderr=write_fd,
            )
            os.close(write_fd)
            write_fd = -1
            start = time.monotonic()
            out, _ = _reap(proc, 15, "blocked stderr delivery")
            elapsed = time.monotonic() - start
            assert proc.returncode == 1
            assert elapsed < 15
            assert b"STRAY-MARKER-print-leak" not in out
        finally:
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
            try:
                os.close(read_fd)
            except OSError:
                pass


class TestSealFlushFailureLine:
    """F4 (should-fix): the timeout and _seal_fail paths leave the
    fixed line when a failed final flush is possible."""

    def _run_stdout_full(self, tmp_path, scenario, cmd="invoke"):
        home = tmp_path / f"home-full-{scenario}-{cmd}"
        home.mkdir(exist_ok=True)
        full = os.open("/dev/full", os.O_WRONLY)
        try:
            proc = subprocess.Popen(
                [sys.executable, str(HELPER), str(home), scenario, cmd],
                stdout=full,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        finally:
            os.close(full)
        _, err = _reap(proc, 30, f"{scenario}/{cmd} to /dev/full")
        return proc.returncode, err

    def test_timeout_stdout_full_exits_1_with_fixed_line(
        self, tmp_path
    ):
        code, err = self._run_stdout_full(tmp_path, "timeout_spin")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_seal_fail_stdout_full_exits_1_with_fixed_line(
        self, tmp_path
    ):
        code, err = self._run_stdout_full(tmp_path, "body_raise")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_dropins_out_stdout_full_exits_1_with_fixed_line(
        self, tmp_path
    ):
        # W4: dropins --out to /dev/full exits 1 with the fixed line.
        code, err = self._run_stdout_full(tmp_path, "quiet", "dropins-out")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_dry_run_stdout_full_exits_1_with_fixed_line(self, tmp_path):
        # W4: install-commands --dry-run to /dev/full exits 1 the same way.
        code, err = self._run_stdout_full(tmp_path, "quiet", "dry-run")
        assert code == 1
        lines = err.decode("utf-8").splitlines()
        assert lines and lines[-1] == FIXED_LINE


class TestDescribeDoctorMatrix:
    """F5 (should-fix): describe/doctor success bytes and _seal_fail."""

    def test_describe_success(self, tmp_path):
        proc = run_helper(tmp_path, "quiet", cmd="describe")
        assert proc.returncode == 0
        assert proc.stderr == b""
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["schema"] == "housesitter.describe/1"
        assert body["module"] == "housesitter"
        modules = body["data"]["modules"]
        assert len(modules) == 1
        module = modules[0]
        assert module["id"] == "stdio_probe"
        assert module["title"] == "Stdio probe"
        assert module["version"] == "0.1.0"
        assert module["enabled"] is True
        assert module["skill"] is None
        assert module["commands"] == [
            {
                "name": "housesitter.stdio_probe.probe",
                "description": "Read a probe value from the test sensor.",
                "required": [],
                "optional": [],
                "timeout_ms": 30000,
            }
        ]
        assert body["warnings"] == ["SKILL.md for stdio_probe is missing"]
        names = [item["name"] for item in body["data"]["core_commands"]]
        assert names == ["housesitter.describe", "housesitter.doctor"]

    def test_doctor_success(self, tmp_path):
        proc = run_helper(tmp_path, "quiet", cmd="doctor")
        assert proc.returncode == 0
        assert proc.stderr == b""
        body = json.loads(proc.stdout.decode("utf-8"))
        assert body["ok"] is True
        assert body["schema"] == "housesitter.doctor/1"
        data = body["data"]
        assert set(data) == {
            "config", "secrets", "data_dir", "dropins", "sudo",
            "modules", "problems",
        }
        assert data["config"] == {
            "path": "~/config.json", "ok": True, "problem": None,
            "exists": False,
        }
        assert data["secrets"]["ok"] is True
        assert data["modules"] == [
            {
                "id": "stdio_probe", "enabled": True, "check": None,
                "problem": None,
            }
        ]
        assert isinstance(data["sudo"]["in_sudo_group"], bool)
        assert isinstance(data["problems"], int)
        assert body["warnings"] == []

    def test_describe_seal_fail(self, tmp_path):
        proc = run_helper(tmp_path, "body_raise", cmd="describe")
        assert proc.returncode == 1
        assert json.loads(proc.stdout.decode("utf-8")) == FIXED_ENVELOPE
        err_text = proc.stderr.decode("utf-8")
        assert "Traceback" not in err_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE

    def test_doctor_seal_fail(self, tmp_path):
        proc = run_helper(tmp_path, "body_raise", cmd="doctor")
        assert proc.returncode == 1
        assert json.loads(proc.stdout.decode("utf-8")) == FIXED_ENVELOPE
        err_text = proc.stderr.decode("utf-8")
        assert "Traceback" not in err_text
        lines = err_text.splitlines()
        assert lines and lines[-1] == FIXED_LINE


class TestRestoreReraise:
    """F6 (should-fix): restore mode re-raises; everything is put back."""

    def _registry_config(self, tmp_path):
        from housesitter.core.config import Config, ModuleConfig
        from housesitter.core.registry import Registry

        def probe(self, ctx, params):
            return sdk.Result(data={"count": 1})

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "RestoreProbeModule",
            (sdk.Module,),
            {
                "id": "restore_probe",
                "title": "Restore probe",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": probe,
            },
        )
        registry = Registry.from_classes([module_cls])
        config = Config(
            path=str(tmp_path / "config.json"),
            secrets_dir=str(tmp_path / "secrets"),
            identifiers="full",
            retention=500,
            modules={"restore_probe": ModuleConfig(enabled=True, retention=500)},
        )
        return registry, config

    def _snapshot(self):
        from housesitter.core import stdio as stdio_mod

        return {
            "stdio": stdio_mod,
            "fds": (_fd_identity(1), _fd_identity(2)),
            "streams": (
                sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__,
            ),
            "sigint": signal.getsignal(signal.SIGINT),
        }

    def _check_restored(self, before):
        assert before["stdio"].active() is None
        assert (_fd_identity(1), _fd_identity(2)) == before["fds"]
        assert (
            sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__,
        ) == before["streams"]
        assert signal.getsignal(signal.SIGINT) == before["sigint"]

    def test_runtime_error_reraises_and_restores(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod

        def broken(*args, **kwargs):
            raise RuntimeError("simulated unexpected failure")

        monkeypatch.setattr(cli_mod._runner, "run_command", broken)
        registry, config = self._registry_config(tmp_path)
        before = self._snapshot()
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(RuntimeError, match="simulated"):
            cli_mod.main(
                ["invoke", "housesitter.restore_probe.probe",
                 "--params", "{}"],
                env={"HOME": str(tmp_path)},
                stdin=io.BytesIO(b""),
                stdout=out,
                stderr=err,
                _exit=no_exit,
                registry=registry,
                config=config,
            )
        assert out.getvalue() == ""
        assert err.getvalue() == ""
        self._check_restored(before)

    def test_systemexit_zero_import_reraises_and_restores(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod

        def lazy_registry():
            if "stdio_exit_import" in sys.modules:
                del sys.modules["stdio_exit_import"]
            import stdio_exit_import  # noqa: F401 (raises SystemExit(0))

            raise AssertionError("unreachable: import must raise")

        monkeypatch.setattr(cli_mod, "load_registry", lazy_registry)
        registry, config = self._registry_config(tmp_path)
        before = self._snapshot()
        out, err = io.StringIO(), io.StringIO()
        with pytest.raises(SystemExit) as excinfo:
            cli_mod.main(
                ["invoke", "housesitter.restore_probe.probe",
                 "--params", "{}"],
                env={"HOME": str(tmp_path)},
                stdin=io.BytesIO(b""),
                stdout=out,
                stderr=err,
                _exit=no_exit,
                registry=None,
                config=config,
            )
        assert excinfo.value.code == 0
        assert out.getvalue() == ""
        assert err.getvalue() == ""
        self._check_restored(before)


class TestSlowWorkCompletesBeforeDelivery:
    """W1 (blocker): slow legitimate work must finish; the delivery
    watchdog arms only before the first CLI byte, never before slow
    file, subprocess, or handler work."""

    def test_slow_install_commands_completes(self, tmp_path):
        # A real (non-dry-run) install prints only the restart step:
        # per-file progress would go to the reserved real fds via the
        # child. The 7 s sleeping backend must finish first: the exit
        # is 0 with full output only if the watchdog did not fire early.
        start = time.monotonic()
        out, err, code = run_helper_limited(tmp_path, "slow_install", 20)
        elapsed = time.monotonic() - start
        assert code == 0
        assert err == b""
        assert elapsed >= 6
        assert out.decode("utf-8") == (
            "restart with: sudo systemctl restart musegadget\n"
            "warning: restarting musegadget stops any command "
            "Muse is running right now\n"
        )

    def test_slow_doctor_check_completes(self, tmp_path):
        out, err, code = run_helper_limited(
            tmp_path, "slow_check", 20, cmd="doctor-check"
        )
        assert code == 0
        assert err == b""
        body = json.loads(out.decode("utf-8"))
        assert body["ok"] is True
        assert body["data"]["modules"] == [
            {
                "id": "stdio_probe",
                "enabled": True,
                "check": ["check-ok-example"],
                "problem": None,
            }
        ]


class TestGuardDownProductionBound:
    """W2: a guard-down production refusal is watchdog-bounded and ends
    with a hard exit, so a stalled reader cannot hang it."""

    def test_guard_down_full_stdout_exits_1(self, tmp_path):
        read_fd, write_fd = os.pipe()
        try:
            _set_pipe_capacity(read_fd, write_fd, 4096)
            os.write(write_fd, b"x" * 4096)
            home = tmp_path / "home-guard-down"
            proc = _popen_helper(home, "guard_down", stdout=write_fd)
            os.close(write_fd)
            write_fd = -1
            start = time.monotonic()
            _, _ = _reap(proc, 15, "guard-down refusal")
            elapsed = time.monotonic() - start
            assert proc.returncode == 1
            # The 5 s watchdog fired: bounded, not instant, not a hang.
            assert elapsed < 12
        finally:
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
            try:
                os.close(read_fd)
            except OSError:
                pass


    def test_guard_down_slow_install_commands_completes(self, tmp_path):
        # Commands that continue unguarded after the guard-unavailable
        # warning must not run their remaining work under the delivery
        # watchdog: a slow installer (or a sudo password prompt) longer
        # than the watchdog must still finish with exit 0.
        start = time.monotonic()
        out, err, code = run_helper_limited(
            tmp_path, "guard_down_slow_install", 20
        )
        elapsed = time.monotonic() - start
        assert code == 0
        assert elapsed >= 6
        assert err == (
            b"housesitter: housesitter: log: output guard unavailable; "
            b"continuing without it\n"
        )
        assert out.decode("utf-8") == (
            "restart with: sudo systemctl restart musegadget\n"
            "warning: restarting musegadget stops any command "
            "Muse is running right now\n"
        )


class TestStdoutStartedIsSeparate:
    """W3: only a stdout write marks stdout output started. A failed
    stderr write must not suppress the fixed stdout envelope when
    stdout could still carry it: exactly one JSON object, never zero
    when one is deliverable and never two."""

    def test_stray_then_failing_stderr_delivers_envelope(self, tmp_path):
        proc = run_helper(tmp_path, "stray_stderr_raise")
        assert proc.returncode == 1
        objects = _json_objects(proc.stdout)
        assert len(objects) == 1
        assert objects[0] == FIXED_ENVELOPE
        assert proc.stderr == b""

    def test_stray_success_failing_stderr_fails_closed(self, tmp_path):
        # A succeeding handler with stray output whose stderr log line
        # cannot be written fails closed: the fixed failure envelope and
        # exit 1, never exit 0 with an undelivered log line.
        proc = run_helper(tmp_path, "stray_ok_stderr_raise")
        assert proc.returncode == 1
        objects = _json_objects(proc.stdout)
        assert len(objects) == 1
        assert objects[0] == FIXED_ENVELOPE
        assert proc.stderr == b""

    def test_registry_error_failing_stderr_delivers_envelope(self, tmp_path):
        proc = run_helper(tmp_path, "registry_stderr_raise")
        assert proc.returncode == 1
        objects = _json_objects(proc.stdout)
        assert len(objects) == 1
        assert objects[0] == FIXED_ENVELOPE
        assert proc.stderr == b""


class TestFlushFaultIsInFlush:
    """W5: the F1 fault happens in flush(), never in write()."""

    def test_write_delivers_and_first_flush_fails(self):
        real = io.StringIO()
        wrapper = stdio_helper._FlushFailThenRaiseStdout(real)
        # Must not raise: the fault lives in flush, so the CLI's own
        # flush-failure swallow path is what the F1 test exercises.
        wrapper.write('{"ok":true}\n')
        assert real.getvalue() == '{"ok":true}\n'
        with pytest.raises(OSError, match="injected test flush failure"):
            wrapper.flush()
        with pytest.raises(RuntimeError, match="fail after envelope"):
            wrapper.flush()


class TestServeExempt:
    """serve is not a Muse command: no guard, no watchdog, no seal.

    The helper replaces serve_command with a stub that writes via
    sys.stderr.write, print, and fd 2, sleeps past the seal watchdog
    interval, and returns its own code. Exempt means all three lines
    reach the real streams and the process exits with the stub's code.
    """

    def test_serve_writes_reach_real_streams_and_returns_code(
        self, tmp_path
    ):
        proc = run_helper(tmp_path, "serve_probe", cmd="serve")
        assert proc.returncode == stdio_helper.SERVE_EXIT_CODE
        out = proc.stdout.decode("utf-8")
        err = proc.stderr.decode("utf-8")
        assert stdio_helper.MARK_SERVE_OUT in out
        assert stdio_helper.MARK_SERVE_ERR in err
        assert stdio_helper.MARK_SERVE_FD in err

    @staticmethod
    def _serve_registry():
        from housesitter.core.registry import Registry

        command = sdk.Command(
            name="probe",
            handler="probe",
            description="Read a probe value from the test sensor.",
            fields=(sdk.Field("data.count", "integer"),),
        )
        module_cls = type(
            "ServeProbeModule",
            (sdk.Module,),
            {
                "id": "serve_probe",
                "title": "Serve probe",
                "version": "0.1.0",
                "config": {},
                "http_allow": (),
                "commands": (command,),
                "probe": lambda self, ctx, params: sdk.Result(
                    data={"count": 1}
                ),
            },
        )
        return Registry.from_classes([module_cls])

    def test_serve_ignores_guard_unavailable(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod
        from housesitter.core import serve as serve_mod
        from housesitter.core import stdio as stdio_mod

        started = []

        def boom():
            started.append(True)
            raise OSError("injected guard failure")

        monkeypatch.setattr(stdio_mod, "start", boom)
        monkeypatch.setattr(
            serve_mod, "serve_command",
            lambda registry, env, out, err: 5,
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_mod.main(
            ["serve"],
            env={"HOME": str(tmp_path)},
            stdin=io.BytesIO(b""),
            stdout=out,
            stderr=err,
            _exit=no_exit,
            registry=self._serve_registry(),
            config=None,
        )
        assert code == 5
        assert started == []
        assert "output guard unavailable" not in err.getvalue()

    def test_serve_nested_under_guard_returns_normally(
        self, tmp_path, monkeypatch
    ):
        from housesitter.core import cli as cli_mod
        from housesitter.core import serve as serve_mod
        from housesitter.core import stdio as stdio_mod

        monkeypatch.setattr(
            serve_mod, "serve_command",
            lambda registry, env, out, err: 5,
        )
        real_start = stdio_mod.start
        calls = []

        def recording_start():
            calls.append(True)
            return real_start()

        monkeypatch.setattr(stdio_mod, "start", recording_start)
        guard = stdio_mod.start()
        calls.clear()
        try:
            out, err = io.StringIO(), io.StringIO()
            code = cli_mod.main(
                ["serve"],
                env={"HOME": str(tmp_path)},
                stdin=io.BytesIO(b""),
                stdout=out,
                stderr=err,
                _exit=no_exit,
                registry=self._serve_registry(),
                config=None,
            )
            # The outer guard is untouched: still installed afterwards.
            assert stdio_mod.active() is guard
        finally:
            guard.restore()
        assert code == 5
        assert calls == []
        assert "output guard unavailable" not in err.getvalue()
