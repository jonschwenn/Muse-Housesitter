"""Read per-secret files referenced by config (ARCHITECTURE §6).

A secret lives in exactly one file under the secrets directory, e.g.
``~/.config/housesitter/secrets/unifi_password`` (mode 600, dir 700).
This module returns the file's content without logging it. Callers must
never place the value in exception text, logs, envelopes, or saved files.

The file is opened with ``O_NOFOLLOW`` (symlinks are refused, so a
switched path cannot redirect the read) and ``O_NONBLOCK`` (a FIFO
cannot hang the open), then every check — regular file, permission
bits, owner — runs on the open file descriptor via ``fstat``. There is
no stat-then-open window: the bytes returned are the bytes of the file
that was checked. Files larger than 64 KiB are refused.

Seam (later PRs): ``core/config.py`` resolves the secrets directory and
passes it here; ``Context.secret(name)`` calls :func:`read_secret`.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

# Names are file names inside the secrets directory, so they may not
# contain path separators or parent-directory references.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

# Secret files are small by construction; anything bigger is a mistake.
MAX_SECRET_BYTES = 64 * 1024


class SecretError(Exception):
    """The secret could not be read. The message never contains the value."""

    def __init__(self, name: str, problem: str) -> None:
        super().__init__(f"cannot read secret {name!r}: {problem}")
        self.name = name
        self.problem = problem


def read_secret(name: str, secrets_dir: str | os.PathLike[str]) -> str:
    """Return the named secret, stripped of one trailing newline.

    Exactly one trailing ``\\n`` or ``\\r\\n`` is removed; anything else
    (a second newline, a lone ``\\r``, an interior newline) refuses the
    file. Raises :class:`SecretError` when the name is invalid, the file
    is missing or unreadable, the path is a symlink, the file is not a
    regular file, the file is group- or world-readable (any of the
    ``0o077`` permission bits), the file is not owned by the current
    user, or the file holds more than 64 KiB. The raised message carries
    only the name and the problem class, never the secret value or file
    contents.
    """
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise SecretError(str(name), "invalid secret name")
    path = Path(secrets_dir) / name
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise SecretError(name, "no such secret file") from None
    except NotADirectoryError:
        raise SecretError(name, "no such secret file") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SecretError(name, "secret file must not be a symlink") from None
        raise SecretError(name, "cannot read secret file") from None
    try:
        try:
            st = os.fstat(fd)
        except OSError:
            raise SecretError(name, "cannot stat secret file") from None
        if not stat.S_ISREG(st.st_mode):
            raise SecretError(name, "not a regular file")
        if st.st_mode & 0o077:
            raise SecretError(name, "secret file must not be group- or world-readable")
        try:
            uid = os.getuid()
        except AttributeError:  # pragma: no cover - non-POSIX platforms
            uid = None
        if uid is not None and st.st_uid != uid:
            raise SecretError(name, "secret file is not owned by the current user")
        pieces: list[bytes] = []
        total = 0
        while True:
            try:
                block = os.read(fd, 65536)
            except OSError:
                raise SecretError(name, "cannot read secret file") from None
            if not block:
                break
            pieces.append(block)
            total += len(block)
            if total > MAX_SECRET_BYTES:
                raise SecretError(name, "secret file is too large") from None
        try:
            value = b"".join(pieces).decode("utf-8")
        except UnicodeDecodeError:
            raise SecretError(name, "cannot read secret file") from None
    finally:
        os.close(fd)
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value:
        raise SecretError(name, "secret file is empty")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SecretError(name, "secret file must hold a single line")
    return value
