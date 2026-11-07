"""Per-module snapshot storage (ARCHITECTURE section 3, ``core/store.py``).

Each module gets one directory under ``$HOME/.local/share/housesitter/``.
``save`` writes one JSON file atomically (temp file plus a hard link, so
concurrent saves never clobber each other); ``list`` pages them newest
first; ``load`` reads one back by id. Retention is count-based and pruned
after each save. Everything here is core-internal; modules see only the
``sdk.Store`` protocol.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from housesitter import sdk

# Kinds look like ``snapshot``; ids like ``snapshot_20261104T151500Z_3fa1``.
# The regexes live in sdk so the StoredItem checks and this store agree.
_KIND_RE = sdk._STORE_KIND_RE
_ID_RE = sdk._STORE_ID_RE

# A saved file that grew past this is refused on load; saves are small.
MAX_LOAD_BYTES = 32 * 1024 * 1024

# Link-then-unlink retries when the random suffix collides.
_MAX_SAVE_TRIES = 16

_DATA_DIR_REASON = (
    "the data directory is missing, unwritable, or unsafe; run housesitter doctor"
)


def _id_kind(stem: str) -> str | None:
    """The kind group of a store-shaped id, or None. Exact, not a prefix:
    ``snap`` must not match ``snap_full_...`` files."""
    match = _ID_RE.fullmatch(stem)
    return match.group(1) if match is not None else None


class StoreError(Exception):
    """Storage failed. ``code`` is ``not_found`` or ``config_invalid``;
    ``reason`` is author-written and never carries file contents."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


def _uid() -> int | None:
    try:
        return os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX platforms
        return None


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """Where module data lives: ``$HOME/.local/share/housesitter/``."""
    source = os.environ if env is None else env
    home = source.get("HOME", "") or str(Path.home())
    return Path(home) / ".local" / "share" / "housesitter"


def _check_dir(path: Path, *, what: str) -> None:
    """Refuse symlinks and foreign-owned directories."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise StoreError("config_invalid", _DATA_DIR_REASON) from None
    except OSError:
        raise StoreError("config_invalid", _DATA_DIR_REASON) from None
    if stat.S_ISLNK(st.st_mode):
        raise StoreError("config_invalid", _DATA_DIR_REASON)
    uid = _uid()
    if uid is not None and st.st_uid != uid:
        raise StoreError("config_invalid", _DATA_DIR_REASON)


class FileStore:
    """The concrete ``sdk.Store``: one directory per module id."""

    def __init__(
        self,
        module_id: str,
        *,
        env: Mapping[str, str] | None = None,
        data_root: Path | str | None = None,
        retention: int = 500,
        clock: Callable[[], datetime] | None = None,
        suffix: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(module_id, str) or re.fullmatch(r"[a-z][a-z0-9_]*", module_id) is None:
            raise ValueError(f"invalid module id: {module_id!r}")
        if (
            not isinstance(retention, int)
            or isinstance(retention, bool)
            or retention < 0
        ):
            raise ValueError("retention must be an integer >= 0")
        root = Path(data_root) if data_root is not None else globals()["data_root"](env)
        self._root = root
        module_dir = root / module_id
        self._module_id = module_id
        self._dir = module_dir
        self._retention = retention
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._suffix = suffix or (lambda: secrets.token_hex(2))

    @property
    def module_dir(self) -> Path:
        """The module's data directory (created on the first save)."""
        return self._dir

    def _ensure_dir(self, *, create: bool) -> bool:
        """Check (and with ``create``, make) the data root and module dir.

        Directories are created lazily, on the first save, so read-only
        commands never write to the disk. Returns False when the module
        dir does not exist and ``create`` is False. Symlinked or
        foreign-owned directories raise ``config_invalid``. Only the
        save path tightens modes: the data root and the module dir are
        both set to 700 when wider. Reads never chmod anything.
        """
        root = self._root
        module_dir = self._dir
        try:
            if not os.path.lexists(root):
                if not create:
                    return False
                root.mkdir(parents=True, mode=0o700, exist_ok=True)
            _check_dir(root, what="data root")
            if not os.path.lexists(module_dir):
                if not create:
                    return False
                module_dir.mkdir(mode=0o700, exist_ok=True)
            _check_dir(module_dir, what="module data dir")
            if create:
                for path in (root, module_dir):
                    mode = stat.S_IMODE(os.lstat(path).st_mode)
                    if mode & ~0o700:
                        os.chmod(path, 0o700)
        except StoreError:
            raise
        except OSError:
            raise StoreError("config_invalid", _DATA_DIR_REASON) from None
        return True

    def _timestamp(self) -> str:
        now = self._clock()
        if not isinstance(now, datetime):
            raise ValueError("clock must return a datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _new_suffix(self) -> str:
        value = self._suffix()
        if not isinstance(value, str) or len(value) != 4:
            raise ValueError("suffix must be 4 lowercase hex characters")
        for char in value:
            if char not in "0123456789abcdef":
                raise ValueError("suffix must be 4 lowercase hex characters")
        return value

    def save(self, kind: str, obj: dict, label: str | None = None) -> str:
        """Write ``obj`` atomically and return its store-relative id."""
        if not isinstance(kind, str) or _KIND_RE.fullmatch(kind) is None:
            raise ValueError(f"invalid save kind: {kind!r}")
        try:
            payload = json.dumps(
                obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
        except (TypeError, ValueError):
            raise ValueError("saved object must be JSON-serialisable") from None
        raw = payload.encode("utf-8")
        self._ensure_dir(create=True)
        stamp = self._timestamp()
        saved_id = ""
        for _ in range(_MAX_SAVE_TRIES):
            saved_id = f"{kind}_{stamp}_{self._new_suffix()}"
            final = self._dir / (saved_id + ".json")
            tmp = self._dir / f".tmp-{saved_id}-{os.getpid()}.json"
            fd = -1
            try:
                fd = os.open(
                    tmp,
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                    0o600,
                )
            except FileExistsError:
                continue
            except OSError:
                raise StoreError("config_invalid", _DATA_DIR_REASON) from None
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, final)
                except FileExistsError:
                    continue
                except OSError:
                    raise StoreError("config_invalid", _DATA_DIR_REASON) from None
                try:
                    dir_fd = os.open(self._dir, os.O_RDONLY | os.O_DIRECTORY)
                except OSError:
                    pass
                else:
                    try:
                        os.fsync(dir_fd)
                    except OSError:
                        # Some filesystems refuse directory fsyncs; the
                        # save itself already succeeded.
                        pass
                    finally:
                        os.close(dir_fd)
                break
            finally:
                if fd != -1:
                    os.close(fd)
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            raise StoreError("config_invalid", _DATA_DIR_REASON)
        self._prune()
        self._clean_stale_tmps(stamp)
        return saved_id

    def _clean_stale_tmps(self, stamp: str) -> None:
        """Remove ``.tmp-*.json`` files older than an hour.

        Cleans up after saves that died mid-write. The cutoff comes
        from this save's own stamp, so cleanup needs no extra clock
        call. Best effort: lstat only (never follow symlinks), regular
        files only, and every error is ignored so cleanup can never
        fail a save.
        """
        try:
            saved_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return
        cutoff = saved_at.timestamp() - 3600
        try:
            names = os.listdir(self._dir)
        except OSError:
            return
        for name in names:
            if not (name.startswith(".tmp-") and name.endswith(".json")):
                continue
            path = self._dir / name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            try:
                if st.st_mtime < cutoff:
                    os.unlink(path)
            except OSError:
                continue

    def _prune(self) -> None:
        """Keep at most ``retention`` files; ``0`` keeps everything."""
        if self._retention == 0:
            return
        try:
            names = os.listdir(self._dir)
        except OSError:
            return
        owned = [
            name
            for name in names
            if name.endswith(".json") and _ID_RE.fullmatch(name[:-5]) is not None
        ]
        if len(owned) <= self._retention:
            return

        def key(name: str) -> tuple:
            stem = name[:-5]
            return (stem.split("_")[-2], stem)

        owned.sort(key=key)
        for stale in owned[: len(owned) - self._retention]:
            try:
                os.unlink(self._dir / stale)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def list(self, kind: str, offset: int = 0, limit: int = 20) -> sdk.StorePage:
        """Page saved files of one kind, newest first."""
        if not isinstance(kind, str) or _KIND_RE.fullmatch(kind) is None:
            raise ValueError(f"invalid save kind: {kind!r}")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be an integer >= 0")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer 1..100")
        if not self._ensure_dir(create=False):
            return sdk.StorePage(total=0, items=())
        try:
            names = os.listdir(self._dir)
        except OSError:
            raise StoreError("config_invalid", _DATA_DIR_REASON) from None
        matching = sorted(
            (
                name
                for name in names
                if name.endswith(".json")
                and _id_kind(name[:-5]) == kind
            ),
            key=lambda name: (name[:-5].split("_")[-2], name[:-5]),
            reverse=True,
        )
        items = []
        for name in matching[offset : offset + limit]:
            stem = name[:-5]
            collected_at, label = self._read_header(stem)
            items.append(
                sdk.StoredItem(
                    id=stem, kind=kind,
                    collected_at=collected_at, label=label,
                )
            )
        return sdk.StorePage(total=len(matching), items=items)

    def _read_header(self, stem: str) -> tuple:
        """Read ``collected_at``/``label`` strings from a saved file, else None."""
        try:
            fd = os.open(
                self._dir / (stem + ".json"),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except OSError:
            return None, None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                return None, None
            pieces = []
            total = 0
            while True:
                try:
                    block = os.read(fd, 65536)
                except OSError:
                    return None, None
                if not block:
                    break
                pieces.append(block)
                total += len(block)
                if total > MAX_LOAD_BYTES:
                    return None, None
            raw = b"".join(pieces)
        except OSError:
            return None, None
        finally:
            os.close(fd)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, None
        if not isinstance(obj, dict):
            return None, None
        collected_at = obj.get("collected_at")
        label = obj.get("label")
        # Damaged metadata must never raise: anything that is not a
        # short UTC stamp or a short label becomes None.
        if (
            not isinstance(collected_at, str)
            or sdk._STORE_COLLECTED_AT_RE.fullmatch(collected_at) is None
        ):
            collected_at = None
        if not isinstance(label, str) or len(label) > sdk._STORE_LABEL_MAX_LEN:
            label = None
        return collected_at, label

    def load(self, id: str) -> dict:
        """Load one saved result by id; unknown or corrupt ids are not_found."""
        if not isinstance(id, str) or _ID_RE.fullmatch(id) is None:
            # Never echo an unvalidated id: it came from the caller, who may
            # smuggle secrets or hostile text into it.
            raise StoreError("not_found", "saved result not found")
        if not self._ensure_dir(create=False):
            raise StoreError("not_found", f"saved result {id!r} not found")
        try:
            fd = os.open(
                self._dir / (id + ".json"),
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            )
        except FileNotFoundError:
            raise StoreError("not_found", f"saved result {id!r} not found") from None
        except NotADirectoryError:
            raise StoreError("not_found", f"saved result {id!r} not found") from None
        except OSError:
            # A symlink (ELOOP), a permission error, or anything else that
            # stops the open: the file is there but cannot be read.
            raise StoreError("not_found", "saved result is unreadable") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise StoreError("not_found", "saved result is unreadable")
            pieces = []
            total = 0
            while True:
                try:
                    block = os.read(fd, 65536)
                except OSError:
                    raise StoreError(
                        "not_found", "saved result is unreadable"
                    ) from None
                if not block:
                    break
                pieces.append(block)
                total += len(block)
                if total > MAX_LOAD_BYTES:
                    raise StoreError("not_found", "saved result is unreadable")
        finally:
            os.close(fd)
        try:
            obj = b"".join(pieces).decode("utf-8")
            data = json.loads(obj)
        except (UnicodeDecodeError, ValueError):
            raise StoreError("not_found", "saved result is unreadable") from None
        if not isinstance(data, dict):
            raise StoreError("not_found", "saved result is unreadable")
        return data
