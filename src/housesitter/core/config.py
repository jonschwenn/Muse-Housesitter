"""Load and validate ``config.json`` (ARCHITECTURE section 6).

The file holds core settings plus one section per module. Secrets never
live here: only names that point at files under the secrets directory.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from housesitter import sdk

MAX_CONFIG_BYTES = 1024 * 1024

_TOP_KEYS = frozenset({"housesitter", "modules"})
_CORE_KEYS = frozenset({"identifiers", "retention", "dashboard"})
_RESERVED_KEYS = frozenset({"enabled", "retention"})


class ConfigError(Exception):
    """The config is missing, unreadable, or invalid.

    ``key`` is a dotted path (``modules.demo_sensor.site``) or the file
    itself; the message never carries the offending value, which may be
    sensitive or hostile.
    """

    code = "config_invalid"

    def __init__(self, key: str, problem: str) -> None:
        super().__init__(f"{key}: {problem}")
        self.key = key
        self.problem = problem


def config_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the config file lives: ``$HOUSESITTER_CONFIG``, else
    ``$HOME/.config/housesitter/config.json``."""
    source = os.environ if env is None else env
    override = source.get("HOUSESITTER_CONFIG", "")
    if override:
        return Path(override)
    home = source.get("HOME", "") or str(Path.home())
    return Path(home) / ".config" / "housesitter" / "config.json"


def _freeze(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return MappingProxyType({key: _freeze(value) for key, value in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(value) for value in obj)
    return obj


def validate_setting(setting: sdk.Setting, value: Any, key: str) -> Any:
    """Check one config value against its Setting, applying defaults.

    Missing (``None``) values resolve to the default, recursively for
    objects; every returned object holds all its declared keys. Raises
    :class:`ConfigError` naming the dotted ``key``, never the value.
    """
    if value is None:
        if setting.required:
            raise ConfigError(key, "required setting is missing")
        if setting.default is None:
            return None
        if setting.type == "object":
            value = dict(setting.default)
        else:
            return sdk._freeze_value(setting.default)
    if not sdk._matches_type(setting.type, value):
        raise ConfigError(key, f"expected {setting.type}")
    if setting.enum is not None and value not in setting.enum:
        raise ConfigError(key, "value is not one of the allowed values")
    if setting.pattern is not None:
        if not isinstance(value, str) or re.fullmatch(setting.pattern, value) is None:
            raise ConfigError(key, "value does not match the required pattern")
    if setting.minimum is not None and value < setting.minimum:
        raise ConfigError(key, "value is below the minimum")
    if setting.maximum is not None and value > setting.maximum:
        raise ConfigError(key, "value is above the maximum")
    if setting.max_len is not None and len(value) > setting.max_len:
        raise ConfigError(key, "value is longer than allowed")
    if setting.type == "object":
        assert setting.properties is not None
        unknown = [name for name in value if name not in setting.properties]
        if unknown:
            raise ConfigError(f"{key}.{unknown[0]}", "unknown setting")
        resolved = {}
        for name, sub in setting.properties.items():
            resolved[name] = validate_setting(
                sub, value.get(name), f"{key}.{name}"
            )
        return _freeze(resolved)
    return _freeze(value) if isinstance(value, Mapping) else value


def _check_retention(value: Any, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(key, "retention must be an integer >= 0")
    return value


# Dashboard server settings (docs/DASHBOARD.md §5). All optional; the
# defaults bind the read-only server to loopback only.
_DASHBOARD_KEYS = frozenset({"bind", "port", "allowed_hosts", "house_label"})

# A strict hostname: labels of letters, digits, and hyphens, dots between
# them, no leading or trailing hyphen or dot, at most 253 characters.
# Anything else must be an IP literal (checked with ipaddress).
_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z"
)

_MAX_ALLOWED_HOSTS = 32
_MAX_HOUSE_LABEL_LEN = 64


@dataclass(frozen=True)
class DashboardConfig:
    """The ``housesitter.dashboard`` section: bind, port, Host allow-list,
    and the owner-chosen house label shown on the dashboard."""

    bind: str = "127.0.0.1"
    port: int = 8080
    allowed_hosts: tuple = ()
    house_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "allowed_hosts", tuple(self.allowed_hosts)
        )


def _validate_dashboard(section: Any, key: str = "housesitter.dashboard",
                        ) -> DashboardConfig:
    """Check the dashboard section, applying defaults. Errors name the key
    and the rule, never the offending value."""
    if section is None:
        return DashboardConfig()
    if not isinstance(section, dict):
        raise ConfigError(key, "section must be an object")
    for name in section:
        if name not in _DASHBOARD_KEYS:
            raise ConfigError(f"{key}.{name}", "unknown setting")
    bind = section.get("bind", "127.0.0.1")
    if not isinstance(bind, str):
        raise ConfigError(f"{key}.bind", "bind must be an IP address literal")
    try:
        ipaddress.ip_address(bind)
    except ValueError:
        raise ConfigError(
            f"{key}.bind", "bind must be an IP address literal"
        ) from None
    port = section.get("port", 8080)
    if (
        isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
    ):
        raise ConfigError(
            f"{key}.port", "port must be an integer 1..65535"
        )
    allowed = section.get("allowed_hosts", [])
    if (
        isinstance(allowed, (str, bytes))
        or not isinstance(allowed, (list, tuple))
        or len(allowed) > _MAX_ALLOWED_HOSTS
    ):
        raise ConfigError(
            f"{key}.allowed_hosts",
            "allowed_hosts must be a list of at most 32 hostnames",
        )
    for entry in allowed:
        if not isinstance(entry, str):
            raise ConfigError(
                f"{key}.allowed_hosts",
                "allowed_hosts must be a list of at most 32 hostnames",
            )
        try:
            ipaddress.ip_address(entry)
        except ValueError:
            if _HOSTNAME_RE.fullmatch(entry) is None:
                raise ConfigError(
                    f"{key}.allowed_hosts",
                    "allowed_hosts must be a list of at most 32 hostnames",
                ) from None
    house_label = section.get("house_label", None)
    if house_label is not None and (
        not isinstance(house_label, str)
        or len(house_label) > _MAX_HOUSE_LABEL_LEN
    ):
        raise ConfigError(
            f"{key}.house_label",
            "house_label must be a string of at most 64 characters",
        )
    return DashboardConfig(
        bind=bind,
        port=port,
        allowed_hosts=tuple(entry.lower() for entry in allowed),
        house_label=house_label,
    )


@dataclass(frozen=True)
class ModuleConfig:
    """One module's validated section: enabled, retention, settings."""

    enabled: bool = False
    retention: int = 500
    settings: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", _freeze(dict(self.settings)))


@dataclass(frozen=True)
class Config:
    """The validated config file: core settings plus per-module sections."""

    path: Path = field(default_factory=lambda: config_path())
    secrets_dir: Path = field(default_factory=lambda: config_path().parent / "secrets")
    identifiers: str = "full"
    retention: int = 500
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    modules: Mapping[str, ModuleConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "modules", MappingProxyType(dict(self.modules)))

    def module(self, module_id: str) -> ModuleConfig:
        """Return one module's section, or a disabled default."""
        found = self.modules.get(module_id)
        if found is not None:
            return found
        return ModuleConfig(enabled=False, retention=self.retention, settings={})


def load_config(
    registry: Any,
    path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Read and validate the config file against the registry's modules.

    A missing file means defaults with no modules enabled. Anything
    unreadable, oversized, or invalid raises :class:`ConfigError`.
    """
    resolved = Path(path) if path is not None else config_path(env)
    secrets_dir = resolved.parent / "secrets"
    if not resolved.exists() or not resolved.is_file():
        if not resolved.exists():
            return Config(
                path=resolved,
                secrets_dir=secrets_dir,
                identifiers="full",
                retention=500,
                modules={},
            )
        raise ConfigError(str(resolved), "config file is not a regular file")
    try:
        with resolved.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError(str(resolved), "config file cannot be read") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(str(resolved), "config file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(str(resolved), "config file is not valid JSON") from None
    try:
        top = json.loads(text)
    except ValueError:
        raise ConfigError(str(resolved), "config file is not valid JSON") from None
    if not isinstance(top, dict):
        raise ConfigError(str(resolved), "config file top level must be an object")
    for name in top:
        if name not in _TOP_KEYS:
            raise ConfigError(name, "unknown top-level key")

    core = top.get("housesitter", {})
    if not isinstance(core, dict):
        raise ConfigError("housesitter", "section must be an object")
    for name in core:
        if name not in _CORE_KEYS:
            raise ConfigError(f"housesitter.{name}", "unknown setting")
    identifiers = core.get("identifiers", "full")
    if identifiers not in ("full", "none"):
        raise ConfigError("housesitter.identifiers", "must be full or none")
    retention = _check_retention(
        core.get("retention", 500), "housesitter.retention"
    )
    dashboard = _validate_dashboard(core.get("dashboard"))

    raw_modules = top.get("modules", {})
    if not isinstance(raw_modules, dict):
        raise ConfigError("modules", "section must be an object")
    modules: dict[str, ModuleConfig] = {}
    for name, section in raw_modules.items():
        try:
            registered = registry.get_module(name)
        except KeyError:
            raise ConfigError(f"modules.{name}", "unknown module") from None
        if not isinstance(section, dict):
            raise ConfigError(f"modules.{name}", "section must be an object")
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"modules.{name}.enabled", "must be a boolean")
        module_retention = _check_retention(
            section.get("retention", retention), f"modules.{name}.retention"
        )
        if enabled is False:
            modules[name] = ModuleConfig(
                enabled=False, retention=module_retention, settings={}
            )
            continue
        for key in section:
            if key not in _RESERVED_KEYS and key not in registered.cls.config:
                raise ConfigError(f"modules.{name}.{key}", "unknown setting")
        settings: dict[str, Any] = {}
        for key, subschema in registered.cls.config.items():
            settings[key] = validate_setting(
                subschema, section.get(key), f"modules.{name}.{key}"
            )
        modules[name] = ModuleConfig(
            enabled=True, retention=module_retention, settings=settings
        )
    return Config(
        path=resolved,
        secrets_dir=secrets_dir,
        identifiers=identifiers,
        retention=retention,
        dashboard=dashboard,
        modules=modules,
    )
