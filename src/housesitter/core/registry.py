"""Find modules and build the command table (ARCHITECTURE section 3).

Built-ins are subpackages of ``housesitter.modules``; third-party
packages expose the ``housesitter.modules`` entry point. Config and the
runner take a Registry; this module never imports module internals.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from housesitter import sdk

_ENTRY_POINT_GROUP = "housesitter.modules"

_ID_RE = re.compile(r"[a-z][a-z0-9_]*")
_FULL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+([.-]?[0-9A-Za-z.]+)?")
_METHODS = frozenset({"GET", "HEAD", "POST"})
# Hard rule 1 guard rail: a POST entry must be a literal path (no regex
# syntax) whose last segment is exactly ``login`` or ``logout``.
_POST_PATH_RE = re.compile(r"(/[A-Za-z0-9_-]+)*/(login|logout)")


class RegistryError(Exception):
    """A module class is invalid, or two modules collide."""


def validate_module_class(cls: Any, *, expected_id: str | None = None) -> None:
    """Check one Module subclass, raising RegistryError on any problem."""
    if not isinstance(cls, type) or not issubclass(cls, sdk.Module):
        raise RegistryError(f"{cls!r} is not a Module subclass")
    name = getattr(cls, "id", "")
    if not isinstance(name, str) or _ID_RE.fullmatch(name) is None:
        raise RegistryError(f"module has an invalid id: {name!r}")
    if expected_id is not None and name != expected_id:
        raise RegistryError(
            f"module id {name!r} does not match its package {expected_id!r}"
        )
    title = getattr(cls, "title", "")
    if not isinstance(title, str) or not title.strip():
        raise RegistryError(f"module {name!r} needs a non-empty title")
    version = getattr(cls, "version", "")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise RegistryError(f"module {name!r} needs a version like 0.1.0")
    config = getattr(cls, "config", None)
    if not isinstance(config, Mapping):
        raise RegistryError(f"module {name!r} config must be a mapping")
    for key, setting in config.items():
        if key in ("enabled", "retention"):
            raise RegistryError(
                f"module {name!r} must not declare reserved config key {key!r}"
            )
        if not isinstance(setting, sdk.Setting):
            raise RegistryError(
                f"module {name!r} config key {key!r} is not a Setting"
            )
    allow = getattr(cls, "http_allow", None)
    if isinstance(allow, (str, bytes)) or not isinstance(allow, Sequence):
        raise RegistryError(f"module {name!r} http_allow must be a sequence")
    for entry in allow:
        if not isinstance(entry, sdk.Allow):
            raise RegistryError(f"module {name!r} allow-list holds {entry!r}")
        if entry.method not in _METHODS:
            raise RegistryError(
                f"module {name!r} allow-list refuses method {entry.method!r}"
            )
        if entry.method == "POST" and _POST_PATH_RE.fullmatch(entry.pattern) is None:
            raise RegistryError(
                f"module {name!r} POST allow-list entry must be a literal "
                "login or logout path"
            )
    commands = getattr(cls, "commands", None)
    if (
        isinstance(commands, (str, bytes))
        or not isinstance(commands, Sequence)
        or not commands
    ):
        raise RegistryError(f"module {name!r} needs at least one command")
    seen: set[str] = set()
    for command in commands:
        if not isinstance(command, sdk.Command):
            raise RegistryError(f"module {name!r} holds {command!r}, not a Command")
        if command.name in seen:
            raise RegistryError(
                f"module {name!r} repeats command name {command.name!r}"
            )
        seen.add(command.name)
        if not callable(getattr(cls, command.handler, None)):
            raise RegistryError(
                f"module {name!r} has no handler for command {command.name!r}"
            )
        full = f"housesitter.{name}.{command.name}"
        if _FULL_NAME_RE.fullmatch(full) is None or len(full) > 64:
            raise RegistryError(f"module {name!r} full command name is invalid: {full!r}")


@dataclass(frozen=True)
class RegisteredModule:
    """One module in the registry, built-in or third-party."""

    id: str
    cls: type
    third_party: bool = False


@dataclass(frozen=True)
class RegisteredCommand:
    """One command in the registry's command table."""

    full_name: str
    module_id: str
    command: sdk.Command


def _discover_builtin_classes() -> list[type]:
    """Import every ``housesitter.modules.<id>`` subpackage, returning the
    single Module subclass each must define."""
    import pkgutil

    import housesitter.modules as package

    found: list[type] = []
    for info in pkgutil.iter_modules(package.__path__):
        dotted = f"housesitter.modules.{info.name}"
        try:
            import importlib

            module = importlib.import_module(dotted)
        except Exception:
            raise RegistryError(
                f"cannot import built-in module package {info.name!r}"
            ) from None
        candidates = [
            obj
            for obj in vars(module).values()
            if isinstance(obj, type)
            and issubclass(obj, sdk.Module)
            and obj is not sdk.Module
            and obj.__module__.startswith(dotted)
        ]
        if len(candidates) != 1:
            raise RegistryError(
                f"built-in module package {info.name!r} must define one Module"
            )
        validate_module_class(candidates[0], expected_id=info.name)
        found.append(candidates[0])
    return found


def _iter_entry_points() -> list:
    """Entry points in our group, on both the 3.9 dict API and 3.10+ select."""
    import importlib.metadata as metadata

    try:
        points = metadata.entry_points()
    except Exception:
        raise RegistryError("cannot read installed entry points") from None
    if hasattr(points, "select"):
        return list(points.select(group=_ENTRY_POINT_GROUP))
    return list(points.get(_ENTRY_POINT_GROUP, ()))


def _discover_third_party_classes() -> list[tuple[str, type]]:
    """Load third-party Module subclasses as ``(entry point name, class)``."""
    found: list[tuple[str, type]] = []
    for point in _iter_entry_points():
        try:
            loaded = point.load()
        except Exception:
            raise RegistryError(
                f"cannot load entry point {point.name!r}"
            ) from None
        if (
            not isinstance(loaded, type)
            or not issubclass(loaded, sdk.Module)
            or loaded is sdk.Module
        ):
            raise RegistryError(f"entry point {point.name!r} is not a Module")
        if loaded.id != point.name:
            raise RegistryError(
                f"entry point {point.name!r} id {loaded.id!r} does not match"
            )
        validate_module_class(loaded, expected_id=point.name)
        found.append((point.name, loaded))
    return found


class Registry:
    """The module and command table. Build with :meth:`from_classes` in
    tests, or :func:`load_registry` for the real discovery."""

    def __init__(self, entries: Sequence[RegisteredModule]) -> None:
        by_id: dict[str, RegisteredModule] = {}
        table: dict[str, RegisteredCommand] = {}
        for entry in entries:
            if entry.id in by_id:
                raise RegistryError(f"duplicate module id: {entry.id!r}")
            by_id[entry.id] = entry
            for command in entry.cls.commands:
                full = f"housesitter.{entry.id}.{command.name}"
                if full in table:
                    raise RegistryError(f"duplicate command name: {full!r}")
                table[full] = RegisteredCommand(
                    full_name=full, module_id=entry.id, command=command
                )
        self._modules = tuple(
            by_id[key] for key in sorted(by_id)
        )
        self._by_id = by_id
        self._commands: Mapping[str, RegisteredCommand] = MappingProxyType(table)

    @classmethod
    def from_classes(
        cls, classes: Sequence[type], third_party: frozenset = frozenset()
    ) -> Registry:
        """Build a registry from Module subclasses (tests and callers)."""
        entries = []
        for klass in classes:
            validate_module_class(klass)
            entries.append(
                RegisteredModule(
                    id=klass.id, cls=klass, third_party=klass.id in third_party
                )
            )
        return cls(entries)

    @property
    def modules(self) -> tuple:
        """Registered modules, ordered by id."""
        return self._modules

    @property
    def commands(self) -> Mapping[str, RegisteredCommand]:
        """Full command name to registration, read-only."""
        return self._commands

    def get_module(self, module_id: str) -> RegisteredModule:
        """Return one module registration, or raise KeyError."""
        return self._by_id[module_id]

    def get_command(self, full_name: str) -> RegisteredCommand:
        """Return one command registration, or raise KeyError."""
        return self._commands[full_name]


def load_registry(include_entry_points: bool = True) -> Registry:
    """Discover built-in and third-party modules and build the registry."""
    entries: list[RegisteredModule] = []
    for klass in _discover_builtin_classes():
        entries.append(RegisteredModule(id=klass.id, cls=klass))
    if include_entry_points:
        for _, klass in _discover_third_party_classes():
            entries.append(
                RegisteredModule(id=klass.id, cls=klass, third_party=True)
            )
    return Registry(entries)
