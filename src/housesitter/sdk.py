"""The public module API (ARCHITECTURE section 3, ``sdk.py``).

This is the only thing modules import from Housesitter, besides the
standard library and their own package. It describes a module's config,
commands, parameters, output fields, and the context a handler runs with.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, ClassVar, Protocol

from housesitter.core.envelope import ERROR_CODES
from housesitter.core.http import Allow, NotAllowed, Response, UpstreamError

__all__ = [
    "Allow",
    "NotAllowed",
    "UpstreamError",
    "Response",
    "ERROR_CODES",
    "CommandError",
    "Param",
    "Setting",
    "TLS_SETTING",
    "Field",
    "Command",
    "Paging",
    "Result",
    "StoredItem",
    "StorePage",
    "HttpSession",
    "Store",
    "Context",
    "Module",
]

# Parameter types the SDK drop-in format accepts.
_PARAM_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array"})
# Config setting types. Array is not allowed: config is validated with a
# minimal stdlib schema, and lists have no defaults story there.
_SETTING_TYPES = frozenset({"string", "integer", "number", "boolean", "object"})

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_FULL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_PATH_RE = re.compile(r"(data|source)(\.[a-z_][a-z0-9_]*(\[\])?)+")
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+([.-]?[0-9A-Za-z.]+)?")


def _matches_type(type_name: str, value: Any) -> bool:
    """True when a runtime value fits an SDK type name.

    ``bool`` is never an integer or a number, and NaN/inf are never a
    number: both would silently corrupt limits and budgets downstream.
    """
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return (
            isinstance(value, float)
            and not math.isnan(value)
            and not math.isinf(value)
        )
    if type_name == "object":
        return isinstance(value, Mapping)
    if type_name == "array":
        return isinstance(value, (list, tuple))
    return False  # pragma: no cover - callers validate the type name first


def _freeze_value(value: Any) -> Any:
    """Make a default/param value safely read-only (dicts to proxy, lists to tuple)."""
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze_value(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    return value


def _freeze_enum(enum: Sequence | None, type_name: str, what: str) -> tuple | None:
    if enum is None:
        return None
    if isinstance(enum, (str, bytes)) or not isinstance(enum, Sequence):
        raise ValueError(f"{what} enum must be a sequence of {type_name}")
    items = tuple(enum)
    for item in items:
        if not _matches_type(type_name, item):
            raise ValueError(f"{what} enum value {item!r} is not {type_name}")
    return items


def _check_bounds(
    type_name: str, minimum: float | None, maximum: float | None, what: str
) -> None:
    if minimum is None and maximum is None:
        return
    if type_name not in ("integer", "number"):
        raise ValueError(f"{what} minimum/maximum need the integer or number type")
    for label, bound in (("minimum", minimum), ("maximum", maximum)):
        if bound is None:
            continue
        if type_name == "integer":
            if not isinstance(bound, int) or isinstance(bound, bool):
                raise ValueError(f"{what} {label} must be an integer")
        elif (
            not isinstance(bound, (int, float))
            or isinstance(bound, bool)
            or not math.isfinite(bound)
        ):
            raise ValueError(f"{what} {label} must be a finite number")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(f"{what} minimum must not exceed maximum")


def _check_max_len(type_name: str, max_len: int | None, what: str) -> None:
    if max_len is None:
        return
    if type_name != "string":
        raise ValueError(f"{what} max_len needs the string type")
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 1:
        raise ValueError(f"{what} max_len must be a positive integer")


def _check_default(
    type_name: str,
    default: Any,
    enum: tuple | None,
    minimum: float | None,
    maximum: float | None,
    max_len: int | None,
    what: str,
) -> None:
    if not _matches_type(type_name, default):
        raise ValueError(f"{what} default {default!r} is not {type_name}")
    if enum is not None and default not in enum:
        raise ValueError(f"{what} default {default!r} is not one of the enum values")
    if minimum is not None and default < minimum:
        raise ValueError(f"{what} default is below minimum")
    if maximum is not None and default > maximum:
        raise ValueError(f"{what} default is above maximum")
    if max_len is not None and len(default) > max_len:
        raise ValueError(f"{what} default is longer than max_len")


class CommandError(Exception):
    """A handler's way to fail with a stable envelope error code.

    ``code`` must be one of the envelope's stable codes; ``reason`` is
    author-written plain language, never upstream text or secrets.
    """

    def __init__(self, code: str, reason: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {code!r}")
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Param:
    """One command parameter. Richer than the SDK drop-in format, which
    carries only ``type`` and ``description``: the core enforces the rest
    and spells it out via :meth:`drop_in_description`."""

    type: str
    description: str
    enum: Sequence | None = None
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    max_len: int | None = None

    def __post_init__(self) -> None:
        if self.type not in _PARAM_TYPES:
            raise ValueError(f"unknown param type: {self.type!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("param description must be a non-empty string")
        enum = _freeze_enum(self.enum, self.type, "param")
        object.__setattr__(self, "enum", enum)
        _check_bounds(self.type, self.minimum, self.maximum, "param")
        _check_max_len(self.type, self.max_len, "param")
        if self.default is not None:
            _check_default(
                self.type,
                self.default,
                enum,
                self.minimum,
                self.maximum,
                self.max_len,
                "param",
            )
            object.__setattr__(self, "default", _freeze_value(self.default))

    def drop_in_description(self) -> str:
        """Description plus plain-words constraints, deterministic order."""
        parts = [self.description]
        if self.enum is not None:
            parts.append("One of: " + ", ".join(str(e) for e in self.enum) + ".")
        if self.default is not None:
            parts.append(f"Default {self.default}.")
        if self.minimum is not None:
            parts.append(f"Minimum {self.minimum}.")
        if self.maximum is not None:
            parts.append(f"Maximum {self.maximum}.")
        if self.max_len is not None:
            parts.append(f"Maximum length {self.max_len} characters.")
        return " ".join(parts)


@dataclass(frozen=True)
class Setting:
    """One key in a module's config section."""

    type: str
    required: bool = False
    default: Any = None
    enum: Sequence | None = None
    pattern: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    max_len: int | None = None
    properties: Mapping[str, Setting] | None = None

    def __post_init__(self) -> None:
        if self.type not in _SETTING_TYPES:
            raise ValueError(f"unknown setting type: {self.type!r}")
        if not isinstance(self.required, bool):
            raise ValueError("setting required must be a boolean")
        if self.required and self.default is not None:
            raise ValueError("setting cannot be required and have a default")
        if self.pattern is not None and self.type != "string":
            raise ValueError("setting pattern needs the string type")
        compiled = None
        if self.pattern is not None:
            try:
                compiled = re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"setting pattern does not compile: {exc}") from None
        if self.properties is not None:
            if self.type != "object":
                raise ValueError("setting properties need the object type")
            if not isinstance(self.properties, Mapping) or not self.properties:
                raise ValueError("setting properties must be a non-empty mapping")
            for key, sub in self.properties.items():
                if not isinstance(key, str) or not key:
                    raise ValueError("setting property names must be non-empty strings")
                if not isinstance(sub, Setting):
                    raise ValueError(f"setting property {key!r} is not a Setting")
            object.__setattr__(
                self, "properties", MappingProxyType(dict(self.properties))
            )
        elif self.type == "object":
            raise ValueError("object settings need properties")
        enum = _freeze_enum(self.enum, self.type, "setting")
        object.__setattr__(self, "enum", enum)
        _check_bounds(self.type, self.minimum, self.maximum, "setting")
        _check_max_len(self.type, self.max_len, "setting")
        if compiled is not None and self.default is not None:
            if compiled.fullmatch(self.default) is None:
                raise ValueError("setting default does not match pattern")
        if self.default is not None:
            if self.type == "object":
                if not isinstance(self.default, Mapping):
                    raise ValueError("object setting default must be a mapping")
                for key, value in self.default.items():
                    sub = self.properties.get(key) if self.properties else None
                    if sub is None:
                        raise ValueError(f"setting default has unknown key {key!r}")
                    if sub.type == "object":
                        # Nested object defaults: build the child to reuse
                        # every check it runs on its own default.
                        Setting(type="object", default=value, properties=sub.properties)
                        continue
                    _check_default(
                        sub.type, value, sub.enum, sub.minimum, sub.maximum,
                        sub.max_len, f"setting default {key!r}",
                    )
                    if sub.pattern is not None and (
                        re.fullmatch(sub.pattern, value) is None
                    ):
                        raise ValueError(
                            f"setting default {key!r} does not match pattern"
                        )
            else:
                _check_default(
                    self.type, self.default, enum, self.minimum, self.maximum,
                    self.max_len, "setting",
                )
            object.__setattr__(self, "default", _freeze_value(self.default))


# The shared TLS config section: pin a fingerprint or CA file, or say so.
TLS_SETTING = Setting(
    type="object",
    default={},
    properties={
        "ca_file": Setting(type="string"),
        "server_hostname": Setting(type="string"),
        "fingerprint_sha256": Setting(type="string"),
        "insecure": Setting(type="boolean", default=False),
    },
)


@dataclass(frozen=True)
class Field:
    """One declared output value, addressed from the envelope root."""

    path: str
    type: str
    nullable: bool = True
    identifier: bool = False
    untrusted: bool = False
    max_len: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or _PATH_RE.fullmatch(self.path) is None:
            raise ValueError(
                "field path must look like "
                "'data.aps[].signal_dbm' or 'source.flavor'"
            )
        if self.type not in _PARAM_TYPES:
            raise ValueError(f"unknown field type: {self.type!r}")
        for label, flag in (
            ("nullable", self.nullable),
            ("identifier", self.identifier),
            ("untrusted", self.untrusted),
        ):
            if not isinstance(flag, bool):
                raise ValueError(f"field {label} must be a boolean")
        if self.untrusted and self.type != "string":
            raise ValueError("field untrusted is only valid for the string type")
        if (
            not isinstance(self.max_len, int)
            or isinstance(self.max_len, bool)
            or self.max_len < 1
        ):
            raise ValueError("field max_len must be a positive integer")


@dataclass(frozen=True)
class Command:
    """One command a module offers to Muse."""

    name: str
    handler: str
    description: str
    required: Mapping[str, Param] = field(default_factory=dict)
    optional: Mapping[str, Param] = field(default_factory=dict)
    timeout_ms: int = 30_000
    fields: Sequence[Field] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME_RE.fullmatch(self.name) is None:
            raise ValueError(f"invalid command name: {self.name!r}")
        if not isinstance(self.handler, str) or not self.handler.isidentifier():
            raise ValueError(f"invalid command handler: {self.handler!r}")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description) > 1024
        ):
            raise ValueError("command description must be 1..1024 characters")
        for label, mapping in (("required", self.required), ("optional", self.optional)):
            if not isinstance(mapping, Mapping):
                raise ValueError(f"command {label} params must be a mapping")
            for key, param in mapping.items():
                if not isinstance(key, str) or _NAME_RE.fullmatch(key) is None:
                    raise ValueError(f"invalid command param name: {key!r}")
                if not isinstance(param, Param):
                    raise ValueError(f"command param {key!r} is not a Param")
        overlap = set(self.required) & set(self.optional)
        if overlap:
            raise ValueError(f"command params in both required and optional: {overlap}")
        object.__setattr__(self, "required", MappingProxyType(dict(self.required)))
        object.__setattr__(self, "optional", MappingProxyType(dict(self.optional)))
        if (
            not isinstance(self.timeout_ms, int)
            or isinstance(self.timeout_ms, bool)
            or not 5_000 <= self.timeout_ms <= 600_000
        ):
            raise ValueError("command timeout_ms must be 5000..600000")
        if isinstance(self.fields, (str, bytes)) or not isinstance(
            self.fields, Sequence
        ):
            raise ValueError("command fields must be a sequence of Field")
        items = tuple(self.fields)
        for item in items:
            if not isinstance(item, Field):
                raise ValueError("command fields must be Field entries")
        paths = [item.path for item in items]
        if len(set(paths)) != len(paths):
            raise ValueError("command fields must not repeat a path")
        object.__setattr__(self, "fields", items)
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version < 1
        ):
            raise ValueError("command schema_version must be an integer >= 1")


@dataclass(frozen=True)
class Paging:
    """How to fetch the rest of a truncated list, in order."""

    command: str
    lists: Sequence[str] = field(default_factory=tuple)
    params: Mapping[str, Any] = field(default_factory=dict)
    start: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or _NAME_RE.fullmatch(self.command) is None:
            raise ValueError(f"invalid paging command name: {self.command!r}")
        if isinstance(self.lists, (str, bytes)) or not isinstance(self.lists, Sequence):
            raise ValueError("paging lists must be a non-empty sequence of keys")
        names = tuple(self.lists)
        if not names:
            raise ValueError("paging lists must not be empty")
        for name in names:
            if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
                raise ValueError(f"invalid paging list name: {name!r}")
        if len(set(names)) != len(names):
            raise ValueError("paging lists must be unique")
        object.__setattr__(self, "lists", names)
        for label, mapping in (("params", self.params), ("start", self.start)):
            if not isinstance(mapping, Mapping):
                raise ValueError(f"paging {label} must be a mapping")
        for key, value in self.start.items():
            if key not in names:
                raise ValueError(f"paging start key {key!r} is not a listed list")
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"paging start offset for {key!r} must be >= 0")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        object.__setattr__(self, "start", MappingProxyType(dict(self.start)))


@dataclass(frozen=True)
class Result:
    """What a handler returns. Only its own shape is checked here;
    applying fields and the size budget is the runner's job."""

    data: Mapping[str, Any]
    source: Mapping[str, Any] = field(default_factory=dict)
    compact: Mapping[str, Any] | None = None
    paging: Paging | None = None
    warnings: Sequence[str] = ()
    label: str | None = None
    collected_at: datetime | None = None
    save: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.data, Mapping):
            raise ValueError("result data must be a mapping")
        if not isinstance(self.source, Mapping):
            raise ValueError("result source must be a mapping")
        if self.compact is not None and not isinstance(self.compact, Mapping):
            raise ValueError("result compact must be a mapping or None")
        if self.paging is not None and not isinstance(self.paging, Paging):
            raise ValueError("result paging must be a Paging or None")
        if isinstance(self.warnings, (str, bytes)) or not isinstance(
            self.warnings, Sequence
        ):
            raise ValueError("result warnings must be a sequence of strings")
        items = tuple(self.warnings)
        for warning in items:
            if not isinstance(warning, str):
                raise ValueError("result warnings must be strings")
        object.__setattr__(self, "warnings", items)
        if self.label is not None and not isinstance(self.label, str):
            raise ValueError("result label must be a string or None")
        if self.collected_at is not None:
            if not isinstance(self.collected_at, datetime):
                raise ValueError("result collected_at must be a datetime or None")
            if (
                self.collected_at.tzinfo is None
                or self.collected_at.utcoffset() is None
            ):
                raise ValueError("result collected_at must be timezone-aware")
        if self.save is not None:
            if (
                not isinstance(self.save, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", self.save) is None
            ):
                raise ValueError(f"invalid result save kind: {self.save!r}")


@dataclass(frozen=True)
class StoredItem:
    """One saved result in the module's data directory."""

    id: str
    kind: str
    collected_at: str | None = None
    label: str | None = None


@dataclass(frozen=True)
class StorePage:
    """One page of saved results."""

    total: int
    items: Sequence[StoredItem] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.total, int) or isinstance(self.total, bool):
            raise ValueError("store page total must be an integer")
        if self.total < 0:
            raise ValueError("store page total must not be negative")
        if isinstance(self.items, (str, bytes)) or not isinstance(
            self.items, Sequence
        ):
            raise ValueError("store page items must be a sequence of StoredItem")
        items = tuple(self.items)
        for item in items:
            if not isinstance(item, StoredItem):
                raise ValueError("store page items must be StoredItem entries")
        object.__setattr__(self, "items", items)


class HttpSession(Protocol):
    """An allow-listed HTTP session handed to one handler call."""

    def request(
        self, method: str, path: str, *, json: Any = None, headers: Any = None
    ) -> Response:
        """Send one request. 401/403 becomes ``upstream_auth``."""
        ...  # pragma: no cover - interface only

    def get(self, path: str, **kw: Any) -> Response:
        """Send a GET request."""
        ...  # pragma: no cover - interface only

    def post(self, path: str, *, json: Any = None, **kw: Any) -> Response:
        """Send a POST request (allow-listed login/logout only)."""
        ...  # pragma: no cover - interface only

    def clear_auth(self) -> None:
        """Drop in-memory cookies and tokens."""
        ...  # pragma: no cover - interface only

    def close(self) -> None:
        """Release the session."""
        ...  # pragma: no cover - interface only

    def __enter__(self) -> HttpSession:
        ...  # pragma: no cover - interface only

    def __exit__(self, *exc_info: Any) -> None:
        ...  # pragma: no cover - interface only


class Store(Protocol):
    """A module's data directory: save, page, and load saved results."""

    def save(self, kind: str, obj: dict, label: str | None = None) -> str:
        """Save a result, returning its store-relative id."""
        ...  # pragma: no cover - interface only

    def list(self, kind: str, offset: int = 0, limit: int = 20) -> StorePage:
        """Page saved results of one kind."""
        ...  # pragma: no cover - interface only

    def load(self, id: str) -> dict:
        """Load one saved result by id."""
        ...  # pragma: no cover - interface only


class Context(Protocol):
    """Everything a handler may touch. The concrete class lives in core."""

    @property
    def module_id(self) -> str:
        """The module's id, e.g. ``unifi_wifi``."""
        ...  # pragma: no cover - interface only

    @property
    def config(self) -> Mapping[str, Any]:
        """The module's validated config section, read-only."""
        ...  # pragma: no cover - interface only

    @property
    def store(self) -> Store:
        """The module's data directory."""
        ...  # pragma: no cover - interface only

    @property
    def log(self) -> logging.Logger:
        """A logger that redacts secret values it has seen."""
        ...  # pragma: no cover - interface only

    @property
    def deadline(self) -> float:
        """Seconds left before the self-deadline, floored at 0."""
        ...  # pragma: no cover - interface only

    def now(self) -> datetime:
        """Current time, timezone-aware in UTC."""
        ...  # pragma: no cover - interface only

    def secret(self, name: str) -> str:
        """Read a named secret file. Failures become ``secret_unreadable``."""
        ...  # pragma: no cover - interface only

    def http(
        self, base_url: str, *, tls: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> HttpSession:
        """Open an allow-listed session for one scheme, host, and port."""
        ...  # pragma: no cover - interface only


class Module:
    """One source of data. Subclass it, one subclass per package."""

    id: ClassVar[str] = ""
    title: ClassVar[str] = ""
    version: ClassVar[str] = ""
    config: ClassVar[Mapping[str, Setting]] = {}
    http_allow: ClassVar[Sequence[Allow]] = ()
    commands: ClassVar[Sequence[Command]] = ()
    skill_file: ClassVar[str] = "SKILL.md"

    def check(self, ctx: Context) -> list[str]:
        """Optional read-only connectivity check, used by doctor."""
        return []
