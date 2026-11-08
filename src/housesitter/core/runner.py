"""Run one command end to end (ARCHITECTURE §2.1, MODULES §2).

The pipeline: resolve the command, load config, validate params, build
the context (with a real store), call the handler in a worker thread
bounded by the self-deadline, sanitise untrusted fields, apply the
identifier policy, fit the size budget against a stand-in saved id,
save, and build the envelope with the real id. Failures become an
``Outcome`` with a stable code and an author-written reason; the CLI
prints the envelope to stdout and the last stderr line as
``housesitter: <module>: <code>: <reason>``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from housesitter import __version__, sdk
from housesitter.core import builtins as _builtins
from housesitter.core import envelope
from housesitter.core.config import ConfigError, load_config
from housesitter.core.context import ModuleContext
from housesitter.core.http import NotAllowed, UpstreamError
from housesitter.core.store import FileStore, StoreError

# Params arrive as one JSON object; cap the raw input at 1 MiB.
MAX_PARAMS_BYTES = 1024 * 1024

# Warnings are capped at 20 entries; the overflow line names the rest.
MAX_WARNINGS = 20

_TIMEOUT_REASON = "the command did not finish within its time limit"
_FAILED_REASON = "the module failed unexpectedly"
_TOO_BIG_REASON = "the result is too large for the output limit"
_NOT_ALLOWED_REASON = "the module tried an HTTP call outside its allow-list"

_MODULE_RE = re.compile(r"[a-z][a-z0-9_]*")
_FULL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")

# A late worker log must never reach stderr via logging's lastResort: the
# timeout path detaches the runner's stderr handler while the worker is
# still alive, and the CLI prints the error line after run_command
# returns. With a NullHandler on the housesitter logger, propagation
# always finds a handler, so lastResort is never used for these records.
if not any(
    isinstance(item, logging.NullHandler)
    for item in logging.getLogger("housesitter").handlers
):
    logging.getLogger("housesitter").addHandler(logging.NullHandler())


@dataclass
class Outcome:
    """One finished invocation: the envelope plus how to report it."""

    exit_code: int
    envelope: dict
    error_module: str | None = None
    error_code: str | None = None
    error_reason: str | None = None
    worker_alive: bool = False
    log_lines: list = field(default_factory=list)


def _fail(
    module: str, code: str, reason: str, *, worker_alive: bool = False
) -> Outcome:
    return Outcome(
        exit_code=1,
        envelope=envelope.build_error(code, reason),
        error_module=module,
        error_code=code,
        error_reason=reason,
        worker_alive=worker_alive,
    )


def parse_raw_params(raw: bytes | str | dict | None) -> dict:
    """Normalise raw input to a params dict; empty input means ``{}``."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if not isinstance(raw, (bytes, bytearray)):
        raise sdk.CommandError("bad_params", "params must be a JSON object")
    if len(raw) > MAX_PARAMS_BYTES:
        raise sdk.CommandError("bad_params", "params are too large")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise sdk.CommandError("bad_params", "params are not valid JSON") from None
    if not text.strip():
        return {}
    try:
        obj = json.loads(text)
    except ValueError:
        raise sdk.CommandError("bad_params", "params are not valid JSON") from None
    if not isinstance(obj, dict):
        raise sdk.CommandError("bad_params", "params must be a JSON object")
    return obj


def _thaw(value: Any) -> Any:
    """Copy a frozen default back to plain dicts and lists for the handler."""
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _check_value(param: sdk.Param, value: Any, name: str) -> Any:
    if not sdk._matches_type(param.type, value):
        raise sdk.CommandError("bad_params", f"param {name!r} must be {param.type}")
    if sdk._has_non_finite(value):
        raise sdk.CommandError(
            "bad_params", f"param {name!r} holds a non-finite number"
        )
    if param.enum is not None and value not in param.enum:
        raise sdk.CommandError(
            "bad_params", f"param {name!r} is not one of the allowed values"
        )
    if param.minimum is not None and value < param.minimum:
        raise sdk.CommandError("bad_params", f"param {name!r} is below the minimum")
    if param.maximum is not None and value > param.maximum:
        raise sdk.CommandError("bad_params", f"param {name!r} is above the maximum")
    if param.max_len is not None and len(value) > param.max_len:
        raise sdk.CommandError("bad_params", f"param {name!r} is longer than allowed")
    return value


def validate_params(command: sdk.Command, raw: Mapping[str, Any]) -> dict:
    """Check incoming params against the command's declarations.

    Unknown keys, missing required params, null required params, type
    mismatches, and bound violations are all ``bad_params``. A null
    optional counts as absent; absent optionals with a non-``None``
    default are filled in. Reasons name the param and the rule, never
    the value.
    """
    if not isinstance(raw, Mapping):
        raise sdk.CommandError("bad_params", "params must be a JSON object")
    for key in raw:
        if key not in command.required and key not in command.optional:
            raise sdk.CommandError("bad_params", f"unknown parameter {key!r}")
    params: dict[str, Any] = {}
    for name, param in command.required.items():
        if name not in raw or raw[name] is None:
            raise sdk.CommandError(
                "bad_params", f"param {name!r} is a required parameter"
            )
        params[name] = _check_value(param, raw[name], name)
    for name, param in command.optional.items():
        if name not in raw or raw[name] is None:
            if param.default is not None:
                params[name] = _thaw(param.default)
            continue
        params[name] = _check_value(param, raw[name], name)
    return params


# -- field projection (identifier policy, undeclared keys) -----------------


class _FieldMismatch(Exception):
    """A value does not match its declared field; carries the field path."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


_ABSENT: Any = object()


def _split_field_path(path: str) -> list:
    """``data.aps[].signal_dbm`` to ``[(aps, True), (signal_dbm, False)]``."""
    segments = []
    for part in path.split(".")[1:]:
        if part.endswith("[]"):
            segments.append((part[:-2], True))
        else:
            segments.append((part, False))
    return segments


def _build_tree(fields: list) -> dict:
    """Nest field declarations into ``{kids: {(name, is_list): node}}``.

    ``data.x`` and ``data.x[]...`` address different nodes: the first
    declares the list itself (its ``nullable`` and ``identifier`` apply
    to the list value) while the second declares the items.
    """
    root: dict = {"kids": {}, "leaf": None}
    for item in fields:
        node = root
        for name, is_list in _split_field_path(item.path):
            key = (name, is_list)
            kids = node["kids"]
            if key not in kids:
                kids[key] = {"kids": {}, "leaf": None}
            node = kids[key]
        node["leaf"] = item
    return root


def _check_opaque(item: Any, path: str) -> None:
    """Reject strings, objects, and non-finite floats in an opaque array.

    An ``array`` leaf with no declared descendants may hold only
    finite numbers, booleans, null, and nested arrays of those:
    anything else must be declared item-wise so nested strings are
    sanitised and nested identifiers are stripped. (Object leaves
    always declare descendants, so they never reach this check.)
    """
    if isinstance(item, (str, Mapping)):
        raise _FieldMismatch(path)
    if isinstance(item, float):
        if not math.isfinite(item):
            raise _FieldMismatch(path)
        return
    if isinstance(item, (list, tuple)):
        for entry in item:
            _check_opaque(entry, path)
        return
    if item is None or isinstance(item, (bool, int)):
        return
    raise _FieldMismatch(path)  # pragma: no cover - type-checked first


def _project_list(
    node: dict, value: Any, path: str, strip_identifiers: bool
) -> Any:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise _FieldMismatch(path)
    out = []
    for item in value:
        result = _project_node(node, item, path, strip_identifiers)
        if result is not _ABSENT:
            out.append(result)
    return out


def _project_node(
    node: dict, value: Any, path: str, strip_identifiers: bool
) -> Any:
    leaf = node["leaf"]
    kids = node["kids"]
    if leaf is None and not kids:
        # No declared fields under this root (e.g. an empty source):
        # every key is undeclared, so the projection is empty.
        if not isinstance(value, Mapping):
            raise _FieldMismatch(path)
        return {}
    if leaf is not None and leaf.identifier and strip_identifiers:
        return _ABSENT
    if kids:
        if value is None:
            if leaf is not None and not leaf.nullable:
                raise _FieldMismatch(path)
            return None
        if not isinstance(value, Mapping):
            raise _FieldMismatch(path)
        if leaf is not None and not sdk._matches_type(leaf.type, value):
            raise _FieldMismatch(path)
        out: dict[str, Any] = {}
        groups: dict[str, dict] = {}
        for (name, is_list), child in kids.items():
            groups.setdefault(name, {})[
                "items" if is_list else "plain"
            ] = child
        for name, pair in groups.items():
            if name not in value:
                continue
            sub = value[name]
            child_path = path + "." + name
            plain = pair.get("plain")
            items = pair.get("items")
            # Identifier policy first, so a null identifier is left out
            # like any other value instead of surviving as ``null``.
            if strip_identifiers and any(
                part is not None
                and part["leaf"] is not None
                and part["leaf"].identifier
                for part in (plain, items)
            ):
                continue
            if sub is None:
                # A null list or object is allowed unless its own
                # declaration says otherwise. An undeclared (implied)
                # container stays allowed to be null.
                own = plain["leaf"] if plain is not None else None
                if own is not None and not own.nullable:
                    raise _FieldMismatch(child_path)
                out[name] = None
                continue
            if items is not None:
                item_leaf = items["leaf"]
                plain_leaf = plain["leaf"] if plain is not None else None
                if strip_identifiers and (
                    (item_leaf is not None and item_leaf.identifier)
                    or (plain_leaf is not None and plain_leaf.identifier)
                ):
                    continue
                if plain is not None and plain["leaf"] is not None:
                    if not sdk._matches_type(
                        plain["leaf"].type, sub
                    ):
                        raise _FieldMismatch(child_path)
                out[name] = _project_list(
                    items, sub, child_path + "[]", strip_identifiers
                )
                continue
            result = _project_node(
                plain, sub, child_path, strip_identifiers
            )
            if result is not _ABSENT:
                out[name] = result
        return out
    if value is None:
        if not leaf.nullable:
            raise _FieldMismatch(path)
        return None
    if not sdk._matches_type(leaf.type, value):
        raise _FieldMismatch(path)
    if leaf.type == "array":
        # Opaque arrays hold no declared items: every entry must be a
        # plain scalar or a nested array of those. Object leaves always
        # declare descendants, so a leaf object never reaches this point.
        for entry in value:
            _check_opaque(entry, path)
    return value


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_json(item) for item in value)
    return value


def _sanitize_walk(container: Any, segments: list, max_len: int) -> None:
    """Apply ``sanitize_text`` at one untrusted path, mutating in place."""
    if not segments:
        return
    name, is_list = segments[0]
    rest = segments[1:]
    if isinstance(container, Mapping):
        if name not in container:
            return
        if is_list:
            value = container[name]
            if isinstance(value, tuple):
                value = list(value)
                try:
                    container[name] = value
                except TypeError:
                    return
            if not isinstance(value, list):
                return
            for index, item in enumerate(value):
                if rest:
                    if isinstance(item, Mapping):
                        _sanitize_walk(item, rest, max_len)
                elif isinstance(item, str):
                    value[index] = envelope.sanitize_text(item, max_len)
        else:
            value = container[name]
            if rest:
                if isinstance(value, Mapping):
                    _sanitize_walk(value, rest, max_len)
            elif isinstance(value, str):
                try:
                    container[name] = envelope.sanitize_text(value, max_len)
                except TypeError:
                    pass


def sanitize_mapping(data: Mapping[str, Any], fields: list) -> dict:
    """Copy ``data`` with every untrusted declared string sanitised."""
    cleaned = _copy_json(dict(data))
    for item in fields:
        if item.untrusted:
            _sanitize_walk(cleaned, _split_field_path(item.path), item.max_len)
    return cleaned


# -- size budget ------------------------------------------------------------


class _EnvelopeNotJson(Exception):
    """A built envelope holds a value (NaN/Infinity) JSON cannot represent."""


def _envelope_fits(env_dict: dict) -> bool:
    try:
        return envelope.measure(envelope.serialize(env_dict)) <= envelope.BUDGET_BYTES
    except (TypeError, ValueError):
        # NaN/Infinity, or a value or key JSON cannot hold at all (for
        # example a set in ``Paging.params``).
        raise _EnvelopeNotJson from None


class _PagingNotFetchable(Exception):
    """A truncated result whose ``more`` cannot fetch the rest."""


class _NoProgress(Exception):
    """A continuation that keeps zero items of its start section."""

    def __init__(self, section: str) -> None:
        super().__init__(section)
        self.section = section


class _TooBig(Exception):
    """A result that fits no envelope: no paging covers the rest."""


class _TooBigPaging(Exception):
    """A paged result whose smallest page still exceeds the budget."""

    def __init__(self, size: int) -> None:
        super().__init__(str(size))
        self.size = size


# Why truncation may still fail after it is applied: the paging command
# must exist and accept the replay, and ``more.params`` must carry an id.
_PAGING_REASON = "the module's paging cannot fetch the rest of this result"


def _check_paging_fetchable(
    paging: sdk.Paging,
    params: dict,
    *,
    registry: Any,
    is_core: bool,
    module_id: str,
) -> None:
    """Raise :class:`_PagingNotFetchable` unless ``more`` can be replayed.

    The paging command must be registered and declare ``section``
    (string), ``offset`` (integer), and ``id`` (string) parameters, and
    the replay params must carry an id: the saved id when the result
    was saved, else the paging command's own ``Paging.params`` id (for
    a paging command paging its own output). Core commands validate
    against the core command table, not the module registry.
    """
    if is_core:
        try:
            command = _builtins.CORE_COMMANDS[paging.command]
        except KeyError:
            raise _PagingNotFetchable from None
    else:
        try:
            registered = registry.get_command(
                f"housesitter.{module_id}.{paging.command}"
            )
        except KeyError:
            raise _PagingNotFetchable from None
        command = registered.command
    for name, want in (
        ("section", "string"),
        ("offset", "integer"),
        ("id", "string"),
    ):
        param = command.required.get(name, command.optional.get(name))
        if param is None or param.type != want:
            raise _PagingNotFetchable
    if not isinstance(params.get("id"), str) or not params["id"]:
        raise _PagingNotFetchable


def _truncate(
    working: Mapping[str, Any],
    paging: sdk.Paging,
    saved_id: str | None,
    module_id: str,
    fixed: dict,
    *,
    registry: Any = None,
    is_core: bool = False,
) -> tuple | None:
    """Shrink paging lists from the end until the envelope fits.

    Returns ``(data, more)``, or ``None`` when no declared paging list
    is present as a list. Lists are handled in declared order: the
    first list is cut as little as possible (binary search); only when
    it reaches 0 and the envelope is still too big does the next list
    shrink. Raises :class:`_NoProgress` when the chosen section is a
    continuation start that would keep zero items while items remain,
    and :class:`_TooBigPaging` (carrying the smallest page's size) when
    even the envelope with every list emptied does not fit.
    """
    original: dict[str, list] = {}
    for name in paging.lists:
        value = working.get(name)
        if isinstance(value, (list, tuple)):
            original[name] = list(value)
    if not original:
        return None
    order = [name for name in paging.lists if name in original]
    data = dict(working)
    kept = {name: len(original[name]) for name in order}
    first_section: str | None = None

    def build_more(section: str) -> dict:
        remaining = {
            name: len(original[name]) - kept[name]
            for name in order
            if kept[name] < len(original[name])
        }
        params = dict(paging.params)
        if saved_id is not None:
            params["id"] = saved_id
        params["section"] = section
        params["offset"] = paging.start.get(section, 0) + kept[section]
        # ``remaining`` sits beside ``params``, never inside it: ``params``
        # is the exact call to replay, and the paging command does not
        # declare a ``remaining`` parameter.
        command_name = (
            f"housesitter.{paging.command}"
            if is_core
            else f"housesitter.{module_id}.{paging.command}"
        )
        return {
            "command": command_name,
            "params": params,
            "remaining": remaining,
        }

    def fits(data_trial: dict, section: str) -> bool:
        trial = envelope.build_ok(
            data=data_trial, truncated=True, more=build_more(section), **fixed
        )
        return _envelope_fits(trial)

    for name in order:
        items = original[name]
        section = first_section or name
        trial = dict(data)
        trial[name] = []
        kept[name] = 0
        if not fits(trial, section):
            data[name] = []
            # An empty list was not cut: ``more`` names the first list
            # that actually lost items.
            if first_section is None and items:
                first_section = name
            continue
        low, high = 0, len(items)
        while low < high:
            mid = (low + high + 1) // 2
            trial = dict(data)
            trial[name] = items[:mid]
            kept[name] = mid
            if fits(trial, section):
                low = mid
            else:
                high = mid - 1
        data[name] = items[:low]
        kept[name] = low
        if low < len(items) and first_section is None:
            first_section = name
        if first_section is None:
            first_section = section
        # A continuation names its offset in ``paging.start``: keeping
        # zero items of that section while items remain would replay the
        # same page forever, so the run fails instead. (A first page
        # without a start entry for the section may keep zero items,
        # since its ``more`` is a different call.)
        if (
            first_section in paging.start
            and kept[first_section] == 0
            and len(original[first_section]) > 0
        ):
            raise _NoProgress(first_section)
        # Truncation is actually applied here: the paging command must
        # be able to fetch the rest, or the result fails instead.
        more = build_more(first_section)
        _check_paging_fetchable(
            paging, more["params"], registry=registry, is_core=is_core,
            module_id=module_id,
        )
        return data, more
    # Even the smallest page does not fit: ``data``/``kept`` now
    # describe the trial with every list emptied, so measure that
    # envelope exactly as built and report its size. No ``more``
    # loop is possible from here, so this never returns.
    try:
        smallest = envelope.build_ok(
            data=data, truncated=True,
            more=build_more(
                next((name for name in order if original[name]), order[0])
            ),
            **fixed,
        )
        size = envelope.measure(envelope.serialize(smallest))
    except (TypeError, ValueError):
        raise _EnvelopeNotJson from None
    raise _TooBigPaging(size)


# -- logging -----------------------------------------------------------------


def _module_of_record(record: logging.LogRecord) -> str:
    name = record.name or ""
    if name == "housesitter":
        return "housesitter"
    if name.startswith("housesitter."):
        candidate = name.split(".")[1]
        if candidate and _MODULE_RE.fullmatch(candidate) is not None:
            return candidate
    return "housesitter"


class _StderrHandler(logging.Handler):
    """One stderr line per record: ``housesitter: <module>: log: <msg>``."""

    def __init__(self, stream: Any, lines: list | None = None) -> None:
        super().__init__(level=logging.WARNING)
        self._stream = stream
        self._lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:
            text = "<unformattable log message>"
        if not isinstance(text, str):
            text = str(text)
        flat = "".join(
            " "
            if (char.isspace() or unicodedata.category(char) == "Cc")
            else char
            for char in text
        )
        flat = re.sub(r" {2,}", " ", flat)
        line = f"housesitter: {_module_of_record(record)}: log: {flat}"
        if len(line) > 400:
            line = line[:400]
        if self._lines is not None:
            self._lines.append(line)
        try:
            self._stream.write(line + "\n")
            self._stream.flush()
        except Exception:
            pass


# -- the pipeline --------------------------------------------------------------


def _format_collected(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _final_warnings(items: list) -> list:
    seen: list[str] = []
    for warning in items:
        if not isinstance(warning, str):
            continue
        cleaned = envelope.sanitize_text(warning, 200)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    if len(seen) > MAX_WARNINGS:
        omitted = len(seen) - (MAX_WARNINGS - 1)
        seen = seen[: MAX_WARNINGS - 1] + [f"{omitted} more warnings omitted"]
    return seen


def run_command(
    full_name: str,
    raw: bytes | str | dict | None = None,
    *,
    registry: Any,
    env: Mapping[str, str] | None = None,
    config: Any = None,
    now: Callable[[], datetime] | None = None,
    deadline_override_s: float | None = None,
    commands_dir: str | os.PathLike[str] | None = None,
    monotonic: Callable[[], float] | None = None,
    log_stream: Any = None,
    client_factory: Callable[..., Any] | None = None,
) -> Outcome:
    """Run one command and return its outcome (envelope plus exit code)."""
    clock = monotonic or time.monotonic
    env_map = os.environ if env is None else env
    stream = log_stream if log_stream is not None else sys.stderr
    t_start = clock()

    is_core = full_name in ("housesitter.describe", "housesitter.doctor")
    if is_core:
        module_id = "housesitter"
        command = _builtins.CORE_COMMANDS[full_name.split(".")[-1]]
    else:
        try:
            registered = registry.get_command(full_name)
        except KeyError:
            # Echo the name only when it is a well-formed command name:
            # it arrives from the caller (argv), so it is untrusted text.
            if (
                isinstance(full_name, str)
                and len(full_name) <= 64
                and _FULL_NAME_RE.fullmatch(full_name) is not None
            ):
                return _fail(
                    "housesitter", "not_found", f"unknown command {full_name}"
                )
            return _fail("housesitter", "not_found", "unknown command")
        module_id = registered.module_id
        command = registered.command

    if config is None:
        try:
            config = load_config(registry, env=env_map)
        except ConfigError as exc:
            return _fail(module_id, "config_invalid", f"{exc.key}: {exc.problem}")

    if not is_core:
        try:
            module_cfg = config.module(module_id)
        except Exception:
            return _fail(
                module_id, "upstream_unexpected", _FAILED_REASON
            )
        if not module_cfg.enabled:
            return _fail(
                module_id,
                "config_invalid",
                f"module {module_id} is not enabled in the config",
            )
        retention = module_cfg.retention
        settings: Mapping[str, Any] = module_cfg.settings
        try:
            instance: Any = registry.get_module(module_id).cls()
        except Exception:
            return _fail(module_id, "upstream_unexpected", _FAILED_REASON)
    else:
        retention = config.retention
        settings = {}
        instance = _builtins.CoreCommands(
            registry, config, env_map, commands_dir=commands_dir
        )

    try:
        params = validate_params(command, parse_raw_params(raw))
    except sdk.CommandError as exc:
        return _fail(module_id, exc.code, exc.reason)

    try:
        data_store = FileStore(module_id, env=env_map, retention=retention)
    except StoreError as exc:
        return _fail(module_id, exc.code, exc.reason)
    except Exception:
        return _fail(module_id, "upstream_unexpected", _FAILED_REASON)

    if deadline_override_s is not None:
        deadline_monotonic = t_start + deadline_override_s
    else:
        deadline_monotonic = t_start + command.timeout_ms / 1000 - 3

    try:
        ctx = ModuleContext(
            _builtins.CoreModule() if is_core else instance,
            settings,
            secrets_dir=config.secrets_dir,
            deadline_monotonic=deadline_monotonic,
            now=now,
            store=data_store,
            client_factory=client_factory,
        )
    except Exception:
        return _fail(module_id, "upstream_unexpected", _FAILED_REASON)

    handler = getattr(instance, command.handler, None)
    if not callable(handler):
        return _fail(module_id, "upstream_unexpected", _FAILED_REASON)

    logger = logging.getLogger("housesitter")
    lines: list[str] = []
    handler_obj = _StderrHandler(stream, lines)
    logger.addHandler(handler_obj)
    box: dict[str, Any] = {}
    try:
        collected_default = ctx.now()

        def _target() -> None:
            try:
                # Bound method: getattr(instance, command.handler)(ctx, params).
                box["value"] = handler(ctx, params)
            except BaseException as exc:  # noqa: BLE001 - mapped below
                box["error"] = exc

        worker = threading.Thread(target=_target, daemon=True)
        remaining = max(0.0, deadline_monotonic - clock())
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            return Outcome(
                exit_code=1,
                envelope=envelope.build_error("timeout", _TIMEOUT_REASON),
                error_module=module_id,
                error_code="timeout",
                error_reason=_TIMEOUT_REASON,
                worker_alive=True,
                log_lines=lines,
            )
        if "error" in box:
            exc = box["error"]
            if isinstance(exc, sdk.CommandError):
                return _fail(module_id, exc.code, exc.reason)
            if isinstance(exc, UpstreamError):
                return _fail(module_id, exc.code, exc.message)
            if isinstance(exc, NotAllowed):
                return _fail(module_id, "not_allowed", _NOT_ALLOWED_REASON)
            if isinstance(exc, StoreError):
                return _fail(module_id, exc.code, exc.reason)
            return _fail(module_id, "upstream_unexpected", _FAILED_REASON)
        result = box.get("value")
        if not isinstance(result, sdk.Result):
            return _fail(module_id, "upstream_unexpected", _FAILED_REASON)
    finally:
        try:
            handler_obj.flush()
        except Exception:
            pass
        logger.removeHandler(handler_obj)

    label = result.label if isinstance(result.label, str) else None
    if label is not None:
        label = envelope.sanitize_label(label) or None
    collected_at = result.collected_at or collected_default
    try:
        collected_str = _format_collected(collected_at)
    except Exception:
        return _fail(module_id, "upstream_unexpected", _FAILED_REASON)
    warnings = _final_warnings(list(result.warnings) + list(ctx.warnings))

    data_fields = [item for item in command.fields if item.path.startswith("data.")]
    source_fields = [
        item for item in command.fields if item.path.startswith("source.")
    ]
    try:
        clean_data = sanitize_mapping(result.data, data_fields)
        clean_source = sanitize_mapping(result.source, source_fields)
        clean_compact = (
            sanitize_mapping(result.compact, data_fields)
            if result.compact is not None
            else None
        )
    except Exception:
        return _fail(module_id, "upstream_unexpected", _FAILED_REASON)

    if is_core:
        schema_name = f"housesitter.{command.name}/{command.schema_version}"
    else:
        schema_name = (
            f"housesitter.{module_id}.{command.name}/{command.schema_version}"
        )
    strip = config.identifiers == "none"
    data_tree = _build_tree(data_fields)
    source_tree = _build_tree(source_fields)
    try:
        out_data = _project_node(data_tree, clean_data, "data", strip)
        out_source = _project_node(source_tree, clean_source, "source", strip)
        out_compact = (
            _project_node(data_tree, clean_compact, "data", strip)
            if clean_compact is not None
            else None
        )
    except _FieldMismatch as exc:
        return _fail(
            module_id,
            "upstream_unexpected",
            f"module output does not match its declared fields ({exc.path})",
        )
    if out_data is _ABSENT:  # pragma: no cover - root is never identifier-marked
        out_data = {}
    if out_source is _ABSENT:  # pragma: no cover - root is never identifier-marked
        out_source = {}

    module_version = __version__ if is_core else instance.version
    # Fit against a stand-in saved id of exactly the real id's length:
    # store ids are ``<kind>_YYYYMMDDTHHMMSSZ_xxxx`` (ASCII), so the
    # measured size is identical. The fit decides before anything is
    # saved, so projection and fitting failures leave no saved file.
    standin_id = (
        f"{result.save}_00000000T000000Z_0000"
        if result.save is not None
        else None
    )
    fixed = {
        "schema": schema_name,
        "module": module_id,
        "module_version": module_version,
        "housesitter_version": __version__,
        "collected_at": collected_str,
        "source": out_source,
        "warnings": warnings,
        "saved": standin_id,
        "label": label,
    }

    def fit_once(active: dict) -> tuple:
        """Fit projected data to the budget: full, compact, then paging.

        Returns ``(data, truncated, more)``. Raises
        :class:`_PagingNotFetchable`, :class:`_NoProgress`,
        :class:`_TooBigPaging`, :class:`_TooBig`, or
        :class:`_EnvelopeNotJson`.
        """
        candidate = envelope.build_ok(
            data=out_data, truncated=False, more=None, **active
        )
        if _envelope_fits(candidate):
            return out_data, False, None
        if out_compact is not None and out_compact is not _ABSENT:
            candidate = envelope.build_ok(
                data=out_compact, truncated=False, more=None, **active
            )
            if _envelope_fits(candidate):
                return out_compact, False, None
            working = out_compact
        else:
            working = out_data
        if result.paging is None:
            raise _TooBig
        truncated = _truncate(
            working, result.paging, active["saved"], module_id, active,
            registry=registry, is_core=is_core,
        )
        if truncated is None:
            # No declared paging list is present as a list.
            raise _TooBig
        final_data, more = truncated
        final = envelope.build_ok(
            data=final_data, truncated=True, more=more, **active
        )
        if not _envelope_fits(final):  # pragma: no cover - search guarantees fit
            raise _TooBig
        return final_data, True, more

    def fit_failure(exc: Exception) -> Outcome:
        """Map a fitting failure to its stable error (ARCHITECTURE §5)."""
        if isinstance(exc, _PagingNotFetchable):
            reason = _PAGING_REASON
        elif isinstance(exc, _NoProgress):
            reason = (
                f"one item in {exc.section} is larger than the output limit"
            )
        elif isinstance(exc, _TooBigPaging):
            reason = (
                "the result is too large even with paging: "
                f"the part that cannot be paged is {exc.size} bytes; "
                f"the output limit is {envelope.BUDGET_BYTES} bytes"
            )
        else:
            reason = _TOO_BIG_REASON
        return _fail(module_id, "upstream_unexpected", reason)

    try:
        final_data, is_truncated, more = fit_once(fixed)
    except (_PagingNotFetchable, _NoProgress, _TooBigPaging, _TooBig) as exc:
        return fit_failure(exc)
    except _EnvelopeNotJson:
        return _fail(
            module_id, "upstream_unexpected", "the result is not valid JSON"
        )

    if result.save is not None:
        # Only now, after every check passed, save the sanitised full
        # result from before projection (with identifiers and undeclared
        # keys), then re-run the fit with the real id. The real id has
        # the stand-in's length, so the fit decides the same way.
        obj = {
            "schema": schema_name,
            "module": module_id,
            "module_version": (
                __version__ if is_core else instance.version
            ),
            "housesitter_version": __version__,
            "collected_at": collected_str,
            "label": label,
            "source": clean_source,
            "data": clean_data,
            "warnings": warnings,
        }
        try:
            saved_id = data_store.save(result.save, obj, label)
        except StoreError as exc:
            return _fail(module_id, exc.code, exc.reason)
        except Exception:
            return _fail(module_id, "upstream_unexpected", _FAILED_REASON)
        fixed = {**fixed, "saved": saved_id}
        try:
            final_data, is_truncated, more = fit_once(fixed)
        except (
            _PagingNotFetchable, _NoProgress, _TooBigPaging, _TooBig
        ) as exc:  # pragma: no cover - same sizes decide the same fit
            return fit_failure(exc)
        except _EnvelopeNotJson:
            return _fail(
                module_id, "upstream_unexpected",
                "the result is not valid JSON",
            )

    try:
        final = envelope.build_ok(
            data=final_data, truncated=is_truncated, more=more, **fixed
        )
        fits = _envelope_fits(final)
    except _EnvelopeNotJson:
        return _fail(
            module_id, "upstream_unexpected", "the result is not valid JSON"
        )
    if not fits:  # pragma: no cover - fit_once already verified the size
        return _fail(module_id, "upstream_unexpected", _TOO_BIG_REASON)
    return Outcome(exit_code=0, envelope=final, log_lines=lines)


def format_error_line(outcome: Outcome) -> str:
    """Stderr's last line for a failed outcome."""
    assert outcome.error_module and outcome.error_code and outcome.error_reason
    return envelope.format_error(
        outcome.error_module, outcome.error_code, outcome.error_reason
    )
