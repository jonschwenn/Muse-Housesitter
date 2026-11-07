"""SDK drop-in command files (ARCHITECTURE §2.2, ``core/dropins.py``).

``generate`` builds one JSON file per module command plus the core
commands, from the registry's command table. ``validate`` reimplements
the SDK's own drop-in rules (``commands.py``) so a bad file never
reaches the commands directory. ``serialize`` fixes the exact bytes
written to disk and measured for the size budget.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Mapping
from typing import Any

from housesitter import sdk
from housesitter.core import builtins as _builtins

# Housesitter's own budget: 88 KiB across all generated files, measured as
# the sum of compact-JSON UTF-8 bytes (ARCHITECTURE §2.2).
DROPINS_BUDGET_BYTES = 88 * 1024

# The SDK refuses installed files over 64 KiB.
MAX_DROPIN_BYTES = 64 * 1024

_FULL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_PARAM_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_RESERVED_PREFIXES = ("system.", "file.", "device.", "link.")
_ALLOWED_KEYS = frozenset({"description", "exec", "required", "optional", "timeout_ms"})
_SPEC_KEYS = frozenset({"type", "description"})
_SPEC_TYPES = frozenset({"string", "integer", "number", "boolean", "object", "array"})

_WS_RE = re.compile(r"[\x00-\x20\x7f]")


class DropinError(Exception):
    """A drop-in file breaks an SDK rule or the size budget."""


def check_exec_path(path: str | os.PathLike[str]) -> str:
    """Validate the executable path baked into every drop-in ``exec``.

    It must be absolute, normalised (``normpath`` is a fixed point), and
    free of whitespace and control characters. Returns the string form.
    """
    text = os.fspath(path)
    if not isinstance(text, str) or not text:
        raise DropinError("exec path must be a non-empty string")
    if _WS_RE.search(text):
        raise DropinError("exec path must not hold whitespace or control characters")
    if not os.path.isabs(text):
        raise DropinError("exec path must be absolute")
    if os.path.normpath(text) != text:
        raise DropinError("exec path must be normalised")
    return text


def _param_specs(params: Mapping[str, sdk.Param]) -> dict:
    """The SDK carries only type and description per parameter."""
    return {
        name: {"type": param.type, "description": param.drop_in_description()}
        for name, param in params.items()
    }


def command_entry(
    full_name: str, command: sdk.Command, exec_path: str
) -> dict:
    """One drop-in file's content for a single command."""
    return {
        "description": command.description,
        "exec": [exec_path, "invoke", full_name],
        "required": _param_specs(command.required),
        "optional": _param_specs(command.optional),
        "timeout_ms": command.timeout_ms,
    }


def generate(registry: Any, exec_path: str | os.PathLike[str]) -> dict:
    """Build every drop-in file: all module commands plus core commands.

    Returns ``{filename: content}`` with filenames sorted, so output is
    deterministic. Every file passes :func:`validate`, and the total
    measured size stays within :data:`DROPINS_BUDGET_BYTES`; anything
    over is a generation error.
    """
    binary = check_exec_path(exec_path)
    files: dict[str, dict] = {}
    for full_name in sorted(registry.commands):
        registered = registry.commands[full_name]
        files[full_name + ".json"] = command_entry(
            full_name, registered.command, binary
        )
    for short in sorted(_builtins.CORE_COMMANDS):
        full_name = f"housesitter.{short}"
        files[full_name + ".json"] = command_entry(
            full_name, _builtins.CORE_COMMANDS[short], binary
        )
    for filename, content in files.items():
        validate(filename[:-5], content)
    total = sum(len(serialize(content).encode("utf-8")) for content in files.values())
    if total > DROPINS_BUDGET_BYTES:
        raise DropinError(
            f"drop-in files total {total} bytes, over the "
            f"{DROPINS_BUDGET_BYTES} byte budget"
        )
    return files


def serialize(data: dict) -> str:
    """The exact bytes written to disk: compact, sorted, trailing newline."""
    return (
        json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
        + "\n"
    )


def validate(name: str, data: Any) -> None:
    """Check one drop-in file against the SDK's rules; raise DropinError.

    ``name`` is the full command name (without ``.json``); ``data`` is
    the parsed file content. Every rule comes from the SDK's
    ``commands.py``: the name shape and length, the reserved prefixes,
    the allowed keys, the exact parameter spec shape, and the timeout
    bounds. The per-file 64 KiB limit is checked on the serialised bytes.
    """
    if not isinstance(name, str) or _FULL_NAME_RE.fullmatch(name) is None:
        raise DropinError(f"invalid drop-in command name: {name!r}")
    if len(name) > 64:
        raise DropinError(f"drop-in command name is too long: {name!r}")
    for prefix in _RESERVED_PREFIXES:
        if name.startswith(prefix):
            raise DropinError(f"drop-in command name uses a reserved prefix: {name!r}")
    if not isinstance(data, dict):
        raise DropinError(f"drop-in {name!r} must hold a JSON object")
    for key in data:
        if key not in _ALLOWED_KEYS:
            raise DropinError(f"drop-in {name!r} holds an unknown key: {key!r}")
    description = data.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
    ):
        raise DropinError(f"drop-in {name!r} needs a non-blank description")
    if "exec" not in data:
        raise DropinError(f"drop-in {name!r} needs an exec entry")
    exec_entry = data["exec"]
    if (
        not isinstance(exec_entry, list)
        or not exec_entry
        or any(not isinstance(item, str) or not item for item in exec_entry)
    ):
        raise DropinError(f"drop-in {name!r} exec must be a non-empty string list")
    if not os.path.isabs(exec_entry[0]):
        raise DropinError(f"drop-in {name!r} exec must start with an absolute path")
    required = data.get("required", {})
    optional = data.get("optional", {})
    for label, mapping in (("required", required), ("optional", optional)):
        if not isinstance(mapping, dict):
            raise DropinError(f"drop-in {name!r} {label} must be an object")
        for param_name, spec in mapping.items():
            if (
                not isinstance(param_name, str)
                or _PARAM_NAME_RE.fullmatch(param_name) is None
            ):
                raise DropinError(
                    f"drop-in {name!r} has an invalid {label} param name: "
                    f"{param_name!r}"
                )
            if not isinstance(spec, dict) or set(spec) != set(_SPEC_KEYS):
                raise DropinError(
                    f"drop-in {name!r} param {param_name!r} must hold exactly "
                    "type and description"
                )
            if spec["type"] not in _SPEC_TYPES:
                raise DropinError(
                    f"drop-in {name!r} param {param_name!r} has an unknown type"
                )
            if (
                not isinstance(spec["description"], str)
                or not spec["description"].strip()
            ):
                raise DropinError(
                    f"drop-in {name!r} param {param_name!r} needs a description"
                )
    if set(required) & set(optional):
        raise DropinError(f"drop-in {name!r} repeats a param in required and optional")
    if "timeout_ms" in data:
        timeout = data["timeout_ms"]
        if (
            not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 600_000
        ):
            raise DropinError(f"drop-in {name!r} timeout_ms must be 1..600000")
    size = len(serialize(data).encode("utf-8"))
    if size > MAX_DROPIN_BYTES:
        raise DropinError(
            f"drop-in {name!r} is {size} bytes, over the "
            f"{MAX_DROPIN_BYTES} byte limit"
        )


def write_files(files: Mapping[str, dict], out_dir: str | os.PathLike[str]) -> list:
    """Write generated files into ``out_dir`` (temp file plus rename).

    Returns the sorted filenames. Files land with mode 0o644. Each
    temp file is created exclusive and unreadable-by-others under an
    unpredictable name, then renamed over the destination (which
    replaces a symlink instead of following it); a failed write unlinks
    the temp file.
    """
    names = sorted(files)
    for filename in names:
        dest = os.path.join(str(out_dir), filename)
        tmp = f"{dest}.tmp-{secrets.token_hex(8)}"
        fd = os.open(
            tmp,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            0o644,
        )
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(serialize(files[filename]).encode("utf-8"))
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return names
