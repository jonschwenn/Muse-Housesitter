"""Process-wide output guard for stray handler writes.

Handlers run in a worker thread inside the CLI process, so any write to
``sys.stdout``, ``sys.stderr``, or file descriptors 1 and 2 (``print()``,
``os.write(1, ...)``, writes through ``sys.__stdout__``, C-level writes)
would land on the same stdout as the JSON envelope, breaking the SDK's
single-JSON-object contract and its size budget, and could leak secrets
outside the envelope policies. The guard redirects fds 1 and 2 (and the
``sys.stdout``/``sys.stderr``/``sys.__stdout__``/``sys.__stderr__``
objects) into an anonymous in-memory file that discards everything,
counting the bytes. All CLI output goes through the reserved real
streams instead.

The sink is one ``memfd_create`` file (Linux only), reopened once
through ``/proc/self/fd`` so its open file description carries the
kernel's atomic-position lock: fds 1 and 2 and the sink wrappers are
duplicates of that one description, sharing one file offset, so
concurrent writers count exactly and writes never block, no matter who
holds the GIL. The count is that shared write offset, read with
``lseek``: no flush, no lock, no reader thread, no ``select``. A small
daemon trim thread with its own private duplicate periodically
truncates the file to bound memory; the offset is unaffected, so the
count stays exact, and if the trim thread cannot run, nothing blocks.
``O_APPEND`` is cleared on the shared description (a module or library
may set it), because every trim would otherwise rewind the count.

This is a guard rail, not a sandbox: a hostile module could find the
reserved file descriptor numbers and write to them directly. Review backs
it up. While the guard is up, core code must never write to
``sys.stdout``/``sys.stderr`` directly; use the guard's real streams.
"""

from __future__ import annotations

import io
import os
import sys
import threading

try:  # fcntl exists on the Pi (Linux); the guard needs it for _claim_fd.
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

_DEVNULL = "/dev/null"
_SINK_NAME = "housesitter-stray"
_TRIM_INTERVAL_S = 0.1
_JOIN_TIMEOUT_S = 5


def _flush_quietly(stream: object) -> None:
    """Flush a stream, swallowing every failure.

    Used only on streams the core owns at a moment no module code can
    run: the original sys streams once at start (only the main thread
    exists then), and the reserved real streams at seal. It is never
    called on a module-reachable stream, and never from ``count()``: a
    module may replace a stream with one whose ``flush()`` blocks, or
    hold a buffered writer's lock from another thread.
    """
    try:
        stream.flush()  # type: ignore[attr-defined]
    except Exception:
        pass


def _claim_fd(fd: int) -> int:
    """Return a descriptor for the same file, known to be above 2.

    ``os.dup`` and ``os.memfd_create`` return the lowest free
    descriptor, so with a closed fd 0 they can hand back 0, 1, or 2.
    Guard fds must never be standard fds: a guard fd on 0 would turn
    stdin reads into reads from real stdout, and one on 1 or 2 would
    defeat the redirect. Only call this on freshly created fds (never on
    0, 1, or 2 themselves): the original is closed after the move.
    """
    if fd > 2:
        return fd
    if not _HAVE_FCNTL:
        raise OSError("fcntl is unavailable: cannot claim a guard fd")
    try:
        moved = fcntl.fcntl(fd, fcntl.F_DUPFD, 3)
    except BaseException:
        # The fresh fd is ours (never 0, 1, or 2 themselves): close it
        # here so the failed move leaks nothing, then re-raise.
        try:
            os.close(fd)
        except Exception:
            pass
        raise
    try:
        os.close(fd)
    except Exception:
        pass
    return moved


def _clear_append(fd: int) -> None:
    """Clear ``O_APPEND`` on one sink fd.

    The flag lives on the shared open file description, so one call
    covers fds 1 and 2, the sink wrappers, and the trim thread's dup.
    A module or library may set it (``fcntl(F_SETFL)`` on fd 1 or 2);
    left set, every trim would rewind the count.
    """
    if not _HAVE_FCNTL:
        return
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        return
    if flags & os.O_APPEND:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_APPEND)
        except OSError:
            pass


class OutputGuard:
    """Redirects fds 1/2 into a discarding memfd; reserves real streams."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._done = False
        # Whether the reserved real streams flushed and closed cleanly
        # in seal(): a repeat seal() reports the stored outcome.
        self._seal_ok = True
        self._sink_fd = -1
        # The trim thread's own duplicate of the sink: only it closes
        # this number, so a trim that outlives the bounded join can
        # never ftruncate a recycled fd number.
        self._trim_fd = -1
        self._trim_thread: threading.Thread | None = None
        self._saved_out = -1
        self._saved_err = -1
        self._old_stdout: object = None
        self._old_stderr: object = None
        self._old_dunder_out: object = None
        self._old_dunder_err: object = None
        self._sink_out: io.TextIOWrapper | None = None
        self._sink_err: io.TextIOWrapper | None = None
        # Real streams for CLI output; also handed to child processes.
        self.stdout: io.TextIOWrapper | None = None
        self.stderr: io.TextIOWrapper | None = None
        self.real_stdout_fd = -1
        self.real_stderr_fd = -1

    def count(self) -> int:
        """Bytes discarded so far: the shared write offset of the sink.

        Fds 1 and 2 and both sink wrappers refer to the same open file
        description, so every discarded byte advanced this one offset,
        and the trim thread's truncation never moves it. No flush and
        no lock: ``count()`` never touches a module-reachable stream
        and never takes a lock a module thread could hold.
        """
        return os.lseek(self._sink_fd, 0, os.SEEK_CUR)

    def _trim_loop(self, fd: int) -> None:
        """Daemon loop: truncate the sink so discarded bytes use no RAM.

        Memory only, not correctness: the shared offset survives
        truncation, so later writes extend a sparse file and the count
        stays exact. ``fd`` is the thread's private duplicate: it is
        closed here when the loop ends, and this loop never touches a
        number anyone else owns. ``O_APPEND`` is cleared before each
        truncation, since a module may set it at any time. If
        truncation fails (the sink is gone), just stop.
        """
        try:
            while not self._stop.wait(_TRIM_INTERVAL_S):
                try:
                    _clear_append(fd)
                    os.ftruncate(fd, 0)
                except OSError:
                    return
        finally:
            try:
                os.close(fd)
            except Exception:
                pass

    def _join_trim(self) -> None:
        if self._trim_thread is not None:
            self._trim_thread.join(timeout=_JOIN_TIMEOUT_S)
            self._trim_thread = None

    def seal(self, flush_error: str | None = None) -> bool:
        """End the guarded process: real streams flushed, fds 1/2 voided.

        Used at process end in production; the caller follows with
        ``os._exit``, so nothing here may block and no module code may
        run. In particular this never invokes, flushes, closes, or
        finalizes a module-reachable stream: the sys streams and the
        sink wrappers stay exactly as the module left them (a module
        may have replaced a wrapper's ``flush()`` with a blocking
        call, or hold a buffered writer's lock from another thread).
        Only core-owned objects are touched: the trim thread is
        stopped, the reserved real streams are flushed and closed, and
        fds 1/2 are pointed at ``/dev/null`` so late writes from
        handler threads that outlive the command go nowhere.

        Returns True only when the reserved real streams flushed and
        closed cleanly. When the flush fails -- a full or closed
        destination, or even a KeyboardInterrupt landing mid-flush --
        a fixed diagnostic line (``flush_error``, when given) is
        best-effort written to the reserved real stderr first, and the
        caller must exit nonzero even for an otherwise successful
        command: exit 0 is only for fully delivered output. Never
        raises: every step is best-effort so the caller always reaches
        its hard exit. A repeat call reports the stored outcome.
        """
        if self._done:
            return self._seal_ok
        self._done = True
        _clear_active(self)
        try:
            self._stop.set()
            self._join_trim()
        except BaseException:
            # A signal during the join must not skip the flush below:
            # finish delivering the already-chosen output, then report.
            pass
        # The trim thread owns its private dup and closes it itself.
        self._trim_fd = -1
        # Flush buffered CLI output before the real streams go away.
        # These are core-owned: no module ever receives them.
        ok = True
        for stream in (self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.flush()
                except BaseException:
                    ok = False
        if not ok and flush_error is not None and self.stderr is not None:
            try:
                self.stderr.write(flush_error + "\n")
                self.stderr.flush()
            except BaseException:
                pass
        for stream in (self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.close()
                except BaseException:
                    ok = False
        self._seal_ok = ok
        self.stdout = self.stderr = None
        self.real_stdout_fd = self.real_stderr_fd = -1
        seen = set()
        for fd in (self._saved_out, self._saved_err):
            if fd >= 0 and fd not in seen:
                seen.add(fd)
                try:
                    os.close(fd)
                except Exception:
                    pass
        self._saved_out = self._saved_err = -1
        if self._sink_fd >= 0:
            try:
                os.close(self._sink_fd)
            except Exception:
                pass
            self._sink_fd = -1
        try:
            devnull = _claim_fd(os.open(_DEVNULL, os.O_WRONLY))
        except OSError:
            devnull = -1
        if devnull >= 0:
            try:
                os.dup2(devnull, 1)
            except OSError:
                pass
            try:
                os.dup2(devnull, 2)
            except OSError:
                pass
            try:
                os.close(devnull)
            except Exception:
                pass
        # Deliberately untouched: sys.stdout/sys.stderr/sys.__stdout__/
        # sys.__stderr__, the sink wrappers, and the trim thread's
        # private dup (it closes that number itself when it exits).
        # Late module writes land in the sealed sink or the voided fds
        # until os._exit discards everything: handler-started threads
        # and module atexit hooks do not run past the command.
        return ok

    def restore(self) -> None:
        """Undo the redirection: fds and sys streams back as they were.

        Used when the caller injected its own streams (in-process tests),
        so pytest's capture keeps working.
        """
        if self._done:
            return
        self._done = True
        _clear_active(self)
        self._stop.set()
        self._join_trim()
        # The trim thread owns its private dup and closes it itself;
        # drop our record of the number so nobody else closes it.
        self._trim_fd = -1
        # Unconditional: a saved original of None (the interpreter can
        # leave sys.__stdout__ as None) must come back as None, not as
        # a leftover closed wrapper.
        sys.stdout = self._old_stdout  # type: ignore[assignment]
        sys.stderr = self._old_stderr  # type: ignore[assignment]
        sys.__stdout__ = self._old_dunder_out  # type: ignore[assignment]
        sys.__stderr__ = self._old_dunder_err  # type: ignore[assignment]
        self._close_sinks()
        for target, saved in ((1, self._saved_out), (2, self._saved_err)):
            if saved >= 0:
                try:
                    os.dup2(saved, target)
                except OSError:
                    pass
                try:
                    os.close(saved)
                except Exception:
                    pass
        self._saved_out = self._saved_err = -1
        self.real_stdout_fd = self.real_stderr_fd = -1
        if self._sink_fd >= 0:
            try:
                os.close(self._sink_fd)
            except Exception:
                pass
            self._sink_fd = -1
        for stream in (self.stdout, self.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
        self.stdout = self.stderr = None

    def _close_sinks(self) -> None:
        for sink in (self._sink_out, self._sink_err):
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass
        self._sink_out = self._sink_err = None


_ACTIVE: OutputGuard | None = None


def _clear_active(guard: OutputGuard) -> None:
    global _ACTIVE
    if _ACTIVE is guard:
        _ACTIVE = None


def active() -> OutputGuard | None:
    """The currently installed guard, or None when unguarded."""
    return _ACTIVE


def current_real_fds() -> tuple[int, int] | None:
    """Reserved real fds ``(stdout, stderr)`` for child processes, if guarded."""
    guard = _ACTIVE
    if guard is None:
        return None
    return (guard.real_stdout_fd, guard.real_stderr_fd)


def _raise_if_active() -> None:
    if _ACTIVE is not None:
        raise RuntimeError("the stdio guard is already active")


def _ensure_open(fd: int, writable: bool = True) -> None:
    """Point a closed standard fd at /dev/null so creation cannot reuse it.

    Fds 1 and 2 open write-only; fd 0 opens read-only, so a read from
    closed stdin sees EOF instead of failing or finding another stream.
    """
    try:
        os.fstat(fd)
    except OSError:
        pass
    else:
        return
    try:
        fresh = os.open(_DEVNULL, os.O_WRONLY if writable else os.O_RDONLY)
    except OSError:
        return
    try:
        if fresh != fd:
            try:
                os.dup2(fresh, fd)
            except OSError:
                pass
    finally:
        if fresh != fd:
            try:
                os.close(fresh)
            except Exception:
                pass


def _abort_start(guard: OutputGuard) -> None:
    """Undo a failed start: fds 1/2 back, sys streams back, no leaks.

    Runs when ANY step after the first redirect failed, including trim
    thread startup: fds 1/2 may already point at the orphan sink, so the
    saved dups are always put back (a no-op restore when the redirect
    never happened, since they duplicate the originals). Every fd has
    exactly one owner: a raw fd wrapped by a file object belongs to the
    wrapper and is never closed as a raw fd, so each fd is closed
    exactly once and a reused fd number is never double-closed. Sys
    streams go back unconditionally (a saved None is a real original),
    and a cleanup error never masks the original failure.
    """
    sys.stdout = guard._old_stdout  # type: ignore[assignment]
    sys.stderr = guard._old_stderr  # type: ignore[assignment]
    sys.__stdout__ = guard._old_dunder_out  # type: ignore[assignment]
    sys.__stderr__ = guard._old_dunder_err  # type: ignore[assignment]
    for target, saved in ((1, guard._saved_out), (2, guard._saved_err)):
        if saved >= 0:
            try:
                os.dup2(saved, target)
            except OSError:
                pass
    # The trim thread never started here (thread.start is the last
    # fallible step, and its fd is created just before it), so its
    # private dup is still ours to close. If the thread ever were
    # running it would own that number and close it itself.
    guard._stop.set()
    if guard._trim_thread is None and guard._trim_fd >= 0:
        try:
            os.close(guard._trim_fd)
        except Exception:
            pass
    guard._trim_fd = -1
    seen = set()
    for fd in (guard._saved_out, guard._saved_err, guard._sink_fd):
        if fd >= 0 and fd not in seen:
            seen.add(fd)
            try:
                os.close(fd)
            except Exception:
                pass
    for stream in (
        guard.stdout, guard.stderr, guard._sink_out, guard._sink_err,
    ):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
    guard._saved_out = guard._saved_err = -1
    guard._sink_fd = -1
    guard.real_stdout_fd = guard.real_stderr_fd = -1
    guard.stdout = guard.stderr = None
    guard._sink_out = guard._sink_err = None


def _wrap_sink_fd(raw_fd: int, encoding: str, errors: str) -> io.TextIOWrapper:
    """Wrap a sink dup in an unbuffered write-through text stream.

    Ownership of ``raw_fd`` transfers to the returned wrapper: on
    failure the raw fd is closed here, so callers never close it
    themselves and no fd is ever owned twice.
    """
    try:
        binary = open(raw_fd, "wb", buffering=0)
    except BaseException:
        try:
            os.close(raw_fd)
        except Exception:
            pass
        raise
    try:
        return io.TextIOWrapper(
            binary, encoding=encoding, errors=errors, write_through=True,
        )
    except BaseException:
        try:
            binary.close()
        except Exception:
            pass
        raise


def _wrap_real_fd(raw_fd: int, encoding: str, errors: str) -> io.TextIOWrapper:
    """Wrap a reserved-real dup in a text stream; same ownership rule."""
    try:
        return open(raw_fd, "w", encoding=encoding, errors=errors)
    except BaseException:
        try:
            os.close(raw_fd)
        except Exception:
            pass
        raise


def start() -> OutputGuard:
    """Install the guard: fds 1/2 and the sys streams feed a sink.

    Call after ``parse_args`` (argparse's own output stays unguarded)
    and before ``load_registry`` (module import-time output is caught).
    Stays on for the rest of the process: end with ``seal`` in
    production or ``restore`` for in-process use.

    Linux only: without ``os.memfd_create`` there is no sink, so
    ``start()`` raises ``OSError`` (cleanly rolled back) and the CLI's
    refuse-to-run path takes over. The fresh memfd is reopened once
    through ``/proc/self/fd`` for the same reason: the raw memfd open
    file description lacks the kernel's atomic-position lock, so
    concurrent writers sharing one offset would undercount. If the
    reopen fails, ``start()`` fails cleanly the same way.
    """
    global _ACTIVE
    _raise_if_active()
    guard = OutputGuard()
    # Only the main thread exists here, so flushing the original streams
    # once cannot block on module code; nothing is ever flushed again.
    for stream in (
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__
    ):
        _flush_quietly(stream)
    guard._old_stdout = sys.stdout
    guard._old_stderr = sys.stderr
    guard._old_dunder_out = sys.__stdout__
    guard._old_dunder_err = sys.__stderr__
    out_enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    out_errors = getattr(sys.stdout, "errors", None) or "strict"
    err_enc = getattr(sys.stderr, "encoding", None) or out_enc
    err_errors = getattr(sys.stderr, "errors", None) or out_errors
    _ensure_open(1)
    _ensure_open(2)
    _ensure_open(0, writable=False)
    try:
        guard._saved_out = _claim_fd(os.dup(1))
        guard._saved_err = _claim_fd(os.dup(2))
        try:
            memfd_create = os.memfd_create
        except AttributeError:
            raise OSError(
                "os.memfd_create is unavailable: the stdio guard "
                "needs Linux"
            ) from None
        try:
            memfd_flags = os.MFD_CLOEXEC
        except AttributeError:
            raise OSError(
                "os.MFD_CLOEXEC is unavailable: the stdio guard "
                "needs Linux"
            ) from None
        guard._sink_fd = _claim_fd(memfd_create(_SINK_NAME, memfd_flags))
        # Reopen once through /proc: the raw memfd description has no
        # atomic-position lock, so concurrent writers sharing its
        # offset undercount. The reopened description has it; only it
        # is ever duplicated below. Without /proc there is no sink.
        try:
            reopened = os.open(
                f"/proc/self/fd/{guard._sink_fd:d}",
                os.O_RDWR | os.O_CLOEXEC,
            )
        except OSError:
            raise OSError(
                "the stdio guard needs /proc to reopen its sink"
            ) from None
        try:
            os.close(guard._sink_fd)
        except Exception:
            pass
        guard._sink_fd = _claim_fd(reopened)
        _clear_append(guard._sink_fd)
        # Fds 1 and 2 become duplicates of the sink: one open file
        # description, one shared offset, writes that never block.
        os.dup2(guard._sink_fd, 1)
        os.dup2(guard._sink_fd, 2)
        guard.real_stdout_fd = guard._saved_out
        guard.real_stderr_fd = guard._saved_err
        guard.stdout = _wrap_real_fd(
            _claim_fd(os.dup(guard._saved_out)), out_enc, out_errors,
        )
        guard.stderr = _wrap_real_fd(
            _claim_fd(os.dup(guard._saved_err)), err_enc, err_errors,
        )
        # The sink wrappers are duplicates of the same description too,
        # so sys-level and fd-level writes share the counted offset.
        guard._sink_out = _wrap_sink_fd(
            _claim_fd(os.dup(guard._sink_fd)), out_enc, out_errors,
        )
        guard._sink_err = _wrap_sink_fd(
            _claim_fd(os.dup(guard._sink_fd)), err_enc, err_errors,
        )
        # The trim thread gets its own duplicate, which only it ever
        # closes: a trim that outlives the bounded join below can never
        # ftruncate a recycled fd number.
        guard._trim_fd = _claim_fd(os.dup(guard._sink_fd))
        thread = threading.Thread(
            target=guard._trim_loop,
            args=(guard._trim_fd,),
            name="housesitter-stdio-trim",
            daemon=True,
        )
        thread.start()
        guard._trim_thread = thread
        sys.stdout = guard._sink_out  # type: ignore[assignment]
        sys.stderr = guard._sink_err  # type: ignore[assignment]
        sys.__stdout__ = guard._sink_out  # type: ignore[assignment]
        sys.__stderr__ = guard._sink_err  # type: ignore[assignment]
    except BaseException:
        _abort_start(guard)
        raise
    _ACTIVE = guard
    return guard
