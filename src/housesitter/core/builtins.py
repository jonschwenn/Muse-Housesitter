"""Built-in core commands: ``housesitter.describe`` and ``housesitter.doctor``.

These run through the same runner pipeline as module commands (params,
context, fields, budget, envelope). They gather only Housesitter's own
state; ``doctor`` additionally runs each enabled module's read-only
``Module.check`` when asked. Neither ever fails because of what it
finds: findings are data, always ``ok: true``.
"""

from __future__ import annotations

import grp
import importlib.resources
import os
import stat
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from housesitter import __version__, sdk
from housesitter.core import envelope as _envelope
from housesitter.core import store as _store

# A skill guide over this is ignored; the module ships without one.
SKILL_MAX_BYTES = 32 * 1024

DEFAULT_COMMANDS_DIR = "/etc/musegadget/commands.d"

_SUDO_GROUPS = frozenset({"sudo", "wheel", "admin"})


def _param_shapes(params: Mapping[str, sdk.Param]) -> list:
    """One ``{name, type, description}`` entry per parameter, by name.

    Lists keep every string a declared leaf: parameter maps have
    dynamic keys, which field paths cannot address, and opaque objects
    may not hold strings.
    """
    return [
        {
            "name": name,
            "type": param.type,
            "description": param.drop_in_description(),
        }
        for name in sorted(params)
        for param in (params[name],)
    ]


def _command_shape(
    full_name: str, command: sdk.Command
) -> dict:
    """The JSON shape describe uses for one command."""
    return {
        "name": full_name,
        "description": command.description,
        "required": _param_shapes(command.required),
        "optional": _param_shapes(command.optional),
        "timeout_ms": command.timeout_ms,
    }


DESCRIBE_FIELDS = (
    sdk.Field("data.modules[].id", "string"),
    sdk.Field("data.modules[].title", "string"),
    sdk.Field("data.modules[].version", "string"),
    sdk.Field("data.modules[].third_party", "boolean"),
    sdk.Field("data.modules[].enabled", "boolean"),
    sdk.Field("data.modules[].skill", "string", nullable=True),
    sdk.Field("data.modules[].commands[].name", "string"),
    sdk.Field("data.modules[].commands[].description", "string"),
    sdk.Field("data.modules[].commands[].required[].name", "string"),
    sdk.Field("data.modules[].commands[].required[].type", "string"),
    sdk.Field("data.modules[].commands[].required[].description", "string"),
    sdk.Field("data.modules[].commands[].optional[].name", "string"),
    sdk.Field("data.modules[].commands[].optional[].type", "string"),
    sdk.Field("data.modules[].commands[].optional[].description", "string"),
    sdk.Field("data.modules[].commands[].timeout_ms", "integer"),
    sdk.Field("data.core_commands[].name", "string"),
    sdk.Field("data.core_commands[].description", "string"),
    sdk.Field("data.core_commands[].required[].name", "string"),
    sdk.Field("data.core_commands[].required[].type", "string"),
    sdk.Field("data.core_commands[].required[].description", "string"),
    sdk.Field("data.core_commands[].optional[].name", "string"),
    sdk.Field("data.core_commands[].optional[].type", "string"),
    sdk.Field("data.core_commands[].optional[].description", "string"),
    sdk.Field("data.core_commands[].timeout_ms", "integer"),
)

DOCTOR_FIELDS = (
    sdk.Field("data.config.path", "string"),
    sdk.Field("data.config.ok", "boolean"),
    sdk.Field("data.config.problem", "string", nullable=True),
    sdk.Field("data.config.exists", "boolean"),
    sdk.Field("data.secrets.dir", "string"),
    sdk.Field("data.secrets.ok", "boolean"),
    sdk.Field("data.secrets.problem", "string", nullable=True),
    sdk.Field("data.secrets.exists", "boolean"),
    sdk.Field("data.secrets.files[].name", "string"),
    sdk.Field("data.secrets.files[].ok", "boolean"),
    sdk.Field("data.secrets.files[].problem", "string", nullable=True),
    sdk.Field("data.data_dir.path", "string"),
    sdk.Field("data.data_dir.ok", "boolean"),
    sdk.Field("data.data_dir.problem", "string", nullable=True),
    sdk.Field("data.data_dir.exists", "boolean"),
    sdk.Field("data.dropins.dir", "string"),
    sdk.Field("data.dropins.files[].name", "string"),
    sdk.Field("data.dropins.files[].installed", "boolean"),
    sdk.Field("data.dropins.files[].matches", "boolean"),
    sdk.Field("data.dropins.files[].root_owned_644", "boolean"),
    sdk.Field("data.dropins.files[].problem", "string", nullable=True),
    sdk.Field("data.sudo.in_sudo_group", "boolean"),
    sdk.Field("data.modules[].id", "string"),
    sdk.Field("data.modules[].enabled", "boolean"),
    sdk.Field("data.modules[].check[]", "string", nullable=True),
    sdk.Field("data.modules[].problem", "string", nullable=True),
    sdk.Field("data.problems", "integer"),
)

CORE_COMMANDS: Mapping[str, sdk.Command] = {
    "describe": sdk.Command(
        name="describe",
        handler="describe",
        description=(
            "List installed Housesitter modules, their commands and "
            "parameters, and each module's SKILL.md guide. Read-only; "
            "safe to call any time."
        ),
        fields=DESCRIBE_FIELDS,
        timeout_ms=10_000,
    ),
    "doctor": sdk.Command(
        name="doctor",
        handler="doctor",
        description=(
            "Check Housesitter's own setup: config file, secret files, "
            "data directory, installed drop-in files, and the account's "
            "sudo group. Read-only; never fails because of what it finds."
        ),
        optional={
            "check": sdk.Param(
                "boolean",
                "Also run each enabled module's read-only connectivity "
                "check.",
                default=False,
            ),
        },
        fields=DOCTOR_FIELDS,
        timeout_ms=60_000,
    ),
}


class CoreModule(sdk.Module):
    """The pseudo-module behind the core commands.

    Never registered (``housesitter`` is a reserved id): the runner
    builds it directly so core commands get a real context.
    """

    id = "housesitter"
    title = "Housesitter core commands"
    version = __version__


def display_path(path: str | os.PathLike[str], home: str) -> str:
    """Show paths under HOME as ``~/...`` so output is stable across machines."""
    text = os.fspath(path)
    if text == home:
        return "~"
    if home and text.startswith(home + "/"):
        return "~/" + text[len(home) + 1 :]
    return text


def home_of(env: Mapping[str, str] | None) -> str:
    """HOME from the env mapping, falling back to the current home."""
    source = os.environ if env is None else env
    return source.get("HOME", "") or str(Path.home())


def _read_skill_text(module_cls: type, module_id: str) -> tuple:
    """Return ``(text_or_None, warning_or_None)`` for a module's SKILL.md."""
    skill_file = getattr(module_cls, "skill_file", "SKILL.md")
    if (
        not isinstance(skill_file, str)
        or not skill_file
        or skill_file.startswith(".")
        or "/" in skill_file
        or "\\" in skill_file
        or ".." in skill_file
    ):
        return None, f"SKILL.md file name for {module_id} is invalid"
    modname = getattr(module_cls, "__module__", "") or ""
    candidates = []
    if modname:
        candidates.append(modname)
    if "." in modname:
        candidates.append(modname.rpartition(".")[0])
    for candidate in candidates:
        try:
            ref = importlib.resources.files(candidate) / skill_file
        except Exception:
            continue
        try:
            if not ref.is_file():
                continue
        except Exception:
            continue
        try:
            with ref.open("rb") as handle:
                raw = handle.read(SKILL_MAX_BYTES + 1)
        except Exception:
            break
        if len(raw) > SKILL_MAX_BYTES:
            return None, f"SKILL.md for {module_id} is too large"
        return raw.decode("utf-8", errors="replace"), None
    return None, f"SKILL.md for {module_id} is missing"


def _in_sudo_group() -> bool:
    """Whether the current account is in a sudo-capable group (no subprocess)."""
    try:
        gids = os.getgroups()
    except OSError:
        return False
    try:
        names = set()
        for gid in gids:
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except KeyError:
                continue
        return bool(names & _SUDO_GROUPS)
    except Exception:
        return False


def _uid() -> int | None:
    try:
        return os.geteuid()
    except AttributeError:  # pragma: no cover - non-POSIX platforms
        return None


class _ReadOnlyStore:
    """The store a module ``check`` sees: reads work, saves are refused.

    Doctor checks must never write: ``save`` raises ``config_invalid``
    before touching the disk, while ``list`` and ``load`` delegate to
    the real store (which creates nothing on reads).
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def save(self, kind: str, obj: dict, label: str | None = None) -> str:
        del kind, obj, label
        raise _store.StoreError(
            "config_invalid", "doctor checks are read-only"
        )

    def list(self, kind: str, offset: int = 0, limit: int = 20) -> Any:
        return self._store.list(kind, offset=offset, limit=limit)

    def load(self, id: str) -> dict:
        return self._store.load(id)


class CoreCommands:
    """Handlers for the core commands, with injectable dependencies.

    ``registry`` and ``config`` are the live objects; ``env`` supplies
    HOME; ``commands_dir`` overrides the drop-ins directory (tests);
    ``sudo_check`` overrides the group lookup (tests).
    """

    def __init__(
        self,
        registry: Any,
        config: Any,
        env: Mapping[str, str] | None = None,
        *,
        commands_dir: str | os.PathLike[str] | None = None,
        sudo_check: Callable[[], bool] | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._env = os.environ if env is None else env
        self._commands_dir = (
            str(commands_dir) if commands_dir is not None else DEFAULT_COMMANDS_DIR
        )
        self._sudo_check = sudo_check or _in_sudo_group

    # -- describe --------------------------------------------------------

    def describe(
        self, ctx: sdk.Context, params: Mapping[str, Any]
    ) -> sdk.Result:
        """List modules, commands, and skill guides."""
        del ctx, params
        modules = []
        warnings: list[str] = []
        for entry in self._registry.modules:
            module_id = entry.id
            enabled = False
            try:
                enabled = bool(self._config.module(module_id).enabled)
            except Exception:
                enabled = False
            commands = [
                _command_shape(f"housesitter.{module_id}.{cmd.name}", cmd)
                for cmd in entry.cls.commands
            ]
            skill, warning = _read_skill_text(entry.cls, module_id)
            if warning is not None:
                warnings.append(warning)
            modules.append(
                {
                    "id": module_id,
                    "title": entry.cls.title,
                    "version": entry.cls.version,
                    "third_party": bool(entry.third_party),
                    "enabled": enabled,
                    "commands": commands,
                    "skill": skill,
                }
            )
        core_commands = [
            _command_shape(f"housesitter.{short}", CORE_COMMANDS[short])
            for short in sorted(CORE_COMMANDS)
        ]
        return sdk.Result(
            data={"modules": modules, "core_commands": core_commands},
            warnings=warnings,
        )

    # -- doctor ----------------------------------------------------------

    def doctor(
        self, ctx: sdk.Context, params: Mapping[str, Any]
    ) -> sdk.Result:
        """Report Housesitter's own setup as findings; always ok."""
        del ctx
        run_checks = bool(params.get("check", False))
        home = home_of(self._env)
        config_finding = self._check_config(home)
        secrets_finding = self._check_secrets(home)
        data_finding = self._check_data_dir(home)
        dropins_finding = self._check_dropins()
        module_findings = [
            self._check_module(entry, run_checks) for entry in self._registry.modules
        ]
        problems = 0
        if not config_finding["ok"] or config_finding["problem"] is not None:
            problems += 1
        if not secrets_finding["ok"]:
            problems += 1
        problems += sum(1 for item in secrets_finding["files"] if not item["ok"])
        if not data_finding["ok"]:
            problems += 1
        problems += sum(1 for item in dropins_finding["files"] if item["problem"] is not None)
        problems += sum(1 for item in module_findings if item["problem"] is not None)
        try:
            in_sudo = bool(self._sudo_check())
        except Exception:
            in_sudo = False
        return sdk.Result(
            data={
                "config": config_finding,
                "secrets": secrets_finding,
                "data_dir": data_finding,
                "dropins": dropins_finding,
                "sudo": {"in_sudo_group": in_sudo},
                "modules": module_findings,
                "problems": problems,
            }
        )

    def _check_config(self, home: str) -> dict:
        path = str(self._config.path)
        # A missing file is a valid state (defaults, no modules enabled),
        # not a problem; ``exists`` says which case this is.
        exists = os.path.lexists(path)
        return {
            "path": display_path(path, home),
            "ok": True,
            "problem": None,
            "exists": exists,
        }

    def _check_secrets(self, home: str) -> dict:
        secrets_dir = Path(str(self._config.secrets_dir))
        shown = display_path(secrets_dir, home)
        try:
            st = os.lstat(secrets_dir)
        except FileNotFoundError:
            # No secrets yet is fine until a module needs one; the module's
            # own command reports secret_unreadable then.
            return {
                "dir": shown,
                "ok": True,
                "problem": None,
                "exists": False,
                "files": [],
            }
        except OSError:
            return {
                "dir": shown,
                "ok": False,
                "problem": "secrets directory cannot be read",
                "exists": True,
                "files": [],
            }
        if stat.S_ISLNK(st.st_mode):
            return {
                "dir": shown,
                "ok": False,
                "exists": True,
                "problem": "secrets directory must not be a symlink",
                "files": [],
            }
        if not stat.S_ISDIR(st.st_mode):
            return {
                "dir": shown,
                "ok": False,
                "exists": True,
                "problem": "secrets directory is not a directory",
                "files": [],
            }
        uid = _uid()
        if uid is not None and st.st_uid != uid:
            return {
                "dir": shown,
                "ok": False,
                "exists": True,
                "problem": "secrets directory is not owned by the current user",
                "files": [],
            }
        if stat.S_IMODE(st.st_mode) != 0o700:
            return {
                "dir": shown,
                "ok": False,
                "exists": True,
                "problem": "secrets directory must have mode 700",
                "files": [],
            }
        try:
            names = sorted(os.listdir(secrets_dir))
        except OSError:
            return {
                "dir": shown,
                "ok": False,
                "problem": "secrets directory cannot be read",
                "exists": True,
                "files": [],
            }
        files = [self._check_secret_file(secrets_dir, name) for name in names]
        return {"dir": shown, "ok": True, "problem": None, "exists": True, "files": files}

    def _check_secret_file(self, secrets_dir: Path, name: str) -> dict:
        shown = _envelope.sanitize_text(name)
        try:
            st = os.lstat(secrets_dir / name)
        except OSError:
            return {"name": shown, "ok": False, "problem": "cannot be read"}
        if stat.S_ISLNK(st.st_mode):
            return {"name": shown, "ok": False, "problem": "must not be a symlink"}
        if not stat.S_ISREG(st.st_mode):
            return {"name": shown, "ok": False, "problem": "not a regular file"}
        if st.st_mode & 0o077:
            return {
                "name": shown,
                "ok": False,
                "problem": "must not be group- or world-readable",
            }
        uid = _uid()
        if uid is not None and st.st_uid != uid:
            return {
                "name": shown,
                "ok": False,
                "problem": "not owned by the current user",
            }
        return {"name": shown, "ok": True, "problem": None}

    def _check_data_dir(self, home: str) -> dict:
        root = _store.data_root(self._env)
        shown = display_path(root, home)
        if not os.path.lexists(root):
            return {"path": shown, "ok": True, "problem": None, "exists": False}
        try:
            st = os.lstat(root)
        except OSError:
            return {
                "path": shown,
                "ok": False,
                "problem": "data directory cannot be read",
                "exists": True,
            }
        if stat.S_ISLNK(st.st_mode):
            return {
                "path": shown,
                "ok": False,
                "problem": "data directory must not be a symlink",
                "exists": True,
            }
        if not stat.S_ISDIR(st.st_mode):
            return {
                "path": shown,
                "ok": False,
                "problem": "data directory is not a directory",
                "exists": True,
            }
        uid = _uid()
        if uid is not None and st.st_uid != uid:
            return {
                "path": shown,
                "ok": False,
                "problem": "data directory is not owned by the current user",
                "exists": True,
            }
        if stat.S_IMODE(st.st_mode) != 0o700:
            return {
                "path": shown,
                "ok": False,
                "problem": "data directory must have mode 700",
                "exists": True,
            }
        return {"path": shown, "ok": True, "problem": None, "exists": True}

    def _check_dropins(self) -> dict:
        wanted: dict[str, tuple] = {}
        for full_name in sorted(self._registry.commands):
            registered = self._registry.commands[full_name]
            wanted[full_name + ".json"] = (full_name, registered.command)
        for short in sorted(CORE_COMMANDS):
            full_name = f"housesitter.{short}"
            wanted[full_name + ".json"] = (full_name, CORE_COMMANDS[short])
        commands_dir = self._commands_dir
        files = []
        for filename in sorted(wanted):
            full_name, command = wanted[filename]
            path = os.path.join(commands_dir, filename)
            files.append(self._check_dropin_file(path, filename, full_name, command))
        return {"dir": commands_dir, "files": files}

    def _read_dropin_bytes(self, path: str) -> tuple:
        """Read at most 64 KiB + 1 bytes of an installed drop-in file.

        Returns ``(content, None)`` on success, ``(None, problem)`` when
        the file cannot be read or is larger than the SDK's 64 KiB limit.
        Never loads a huge file whole; the open refuses symlinks and
        non-regular files.
        """
        from housesitter.core import dropins as _dropins

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None, "cannot be read"
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None, "cannot be read"
            pieces = []
            total = 0
            while True:
                want = min(65536, _dropins.MAX_DROPIN_BYTES + 1 - total)
                if want <= 0:
                    break
                try:
                    block = os.read(fd, want)
                except OSError:
                    return None, "cannot be read"
                if not block:
                    break
                pieces.append(block)
                total += len(block)
            raw = b"".join(pieces)
        finally:
            os.close(fd)
        if len(raw) > _dropins.MAX_DROPIN_BYTES:
            return None, "file is larger than 64 KiB"
        return raw, None

    def _check_dropin_file(
        self, path: str, filename: str, full_name: str, command: sdk.Command
    ) -> dict:
        from housesitter.core import dropins as _dropins

        try:
            st = os.lstat(path)
        except OSError:
            return {
                "name": filename,
                "installed": False,
                "matches": False,
                "root_owned_644": False,
                "problem": "not installed",
            }
        if stat.S_ISLNK(st.st_mode):
            return {
                "name": filename,
                "installed": True,
                "matches": False,
                "root_owned_644": False,
                "problem": "must not be a symlink",
            }
        root_owned = (
            st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o644
        )
        installed, read_problem = self._read_dropin_bytes(path)
        if read_problem is not None:
            return {
                "name": filename,
                "installed": True,
                "matches": False,
                "root_owned_644": bool(root_owned),
                "problem": read_problem,
            }
        matches = False
        try:
            import json

            parsed = json.loads(installed.decode("utf-8"))
            exec_path = parsed.get("exec", [None])[0]
            if isinstance(exec_path, str) and exec_path:
                expected = _dropins.command_entry(full_name, command, exec_path)
                matches = installed == _dropins.serialize(expected).encode("utf-8")
        except (UnicodeDecodeError, ValueError, AttributeError, IndexError):
            matches = False
        if not matches:
            return {
                "name": filename,
                "installed": True,
                "matches": False,
                "root_owned_644": bool(root_owned),
                "problem": "does not match the generated drop-in",
            }
        if not root_owned:
            return {
                "name": filename,
                "installed": True,
                "matches": True,
                "root_owned_644": False,
                "problem": "installed but not root-owned with mode 644",
            }
        return {
            "name": filename,
            "installed": True,
            "matches": True,
            "root_owned_644": True,
            "problem": None,
        }

    def _check_module(self, entry: Any, run_checks: bool) -> dict:
        module_id = entry.id
        try:
            enabled = bool(self._config.module(module_id).enabled)
        except Exception:
            enabled = False
        check: list | None = None
        problem: str | None = None
        if run_checks and enabled:
            try:
                check, problem = self._run_module_check(entry)
            except Exception as exc:  # noqa: BLE001 - mapped below, never shown
                check, problem = None, _mapped_code(exc)
        return {
            "id": module_id,
            "enabled": enabled,
            "check": check,
            "problem": problem,
        }

    def _run_module_check(self, entry: Any) -> tuple:
        from housesitter.core import context as _ctx_mod

        module_id = entry.id
        try:
            instance = entry.cls()
        except Exception as exc:
            return None, _mapped_code(exc)
        try:
            module_cfg = self._config.module(module_id)
            retention = module_cfg.retention
            settings = module_cfg.settings
        except Exception as exc:
            return None, _mapped_code(exc)
        try:
            data_store = _store.FileStore(
                module_id, env=self._env, retention=retention
            )
        except _store.StoreError as exc:
            return None, exc.code
        except Exception as exc:
            return None, _mapped_code(exc)
        check_ctx = _ctx_mod.ModuleContext(
            instance,
            settings,
            secrets_dir=self._config.secrets_dir,
            deadline_monotonic=time.monotonic() + 55,
            store=_ReadOnlyStore(data_store),
        )
        try:
            raw = instance.check(check_ctx)
        except Exception as exc:
            return None, _mapped_code(exc)
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            return None, "upstream_unexpected"
        items = []
        for value in raw:
            if not isinstance(value, str):
                continue
            cleaned = _envelope.sanitize_text(value, 200)
            if cleaned:
                items.append(cleaned)
        return items[:20], None


def _mapped_code(exc: Exception) -> str:
    """Map a module check failure to a stable code, never its text."""
    from housesitter.core import http as _http

    if isinstance(exc, sdk.CommandError):
        return exc.code
    if isinstance(exc, _http.UpstreamError):
        return exc.code
    if isinstance(exc, _http.NotAllowed):
        return "not_allowed"
    if isinstance(exc, _store.StoreError):
        return exc.code
    try:
        from housesitter.core import secrets as _secrets_mod

        if isinstance(exc, _secrets_mod.SecretError):
            return "secret_unreadable"
    except ImportError:
        pass
    return "upstream_unexpected"


def default_exec_path(argv0: str | None = None) -> str:
    """The exec path baked into drop-ins when ``--exec`` is omitted.

    Uses ``sys.argv[0]`` resolved, but only when it names the
    ``housesitter`` entry point; anything else (``python -m``, a pytest
    path, a hashed venv binary) must pass ``--exec`` explicitly.
    """
    from housesitter.core import dropins as _dropins

    candidate = argv0 if argv0 is not None else sys.argv[0]
    resolved = str(Path(candidate).resolve())
    if Path(resolved).name != "housesitter":
        raise _dropins.DropinError(
            "cannot tell the installed housesitter path; pass --exec PATH"
        )
    return _dropins.check_exec_path(resolved)
