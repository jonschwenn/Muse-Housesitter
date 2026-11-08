"""Independent re-checks of every conformance rule.

Each check is a plain function returning a list of problem strings
(empty means pass) plus a count of items examined. The checks
re-implement the ARCHITECTURE and MODULES rules from the spec text;
they never call the core validators they shadow (``dropins.validate``,
``runner`` internals) except where the plan explicitly allows shared
helpers. Test files in this directory parametrize every check over
each module under test and assert no problems AND items examined > 0.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import housesitter
from housesitter.core import builtins, dropins, envelope
from housesitter.core import http as http_core

BUDGET_BYTES = 88 * 1024
MAX_DROPIN_BYTES = 64 * 1024

_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")
_FULL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_RESERVED_PREFIXES = ("system.", "file.", "device.", "link.")
_COLLECTED_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# The store's id shape (ARCHITECTURE §3.3): <kind>_<YYYYMMDDTHHMMSSZ>_<4 hex>.
_STORE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,31}_\d{8}T\d{6}Z_[0-9a-f]{4}")
_PLAIN_WORD_RE = re.compile(r"[a-z]+")

_MAC_RE = re.compile(
    r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}"
    r"|(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(
    r"[0-9A-Fa-f:]*::[0-9A-Fa-f:]*"
    r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
)

_PARAM_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "object", "array"}
)
_DROPIN_KEYS = frozenset({"description", "exec", "required", "optional", "timeout_ms"})


@dataclass
class CheckResult:
    """One check's findings plus how many items it examined."""

    problems: list
    examined: int


def check_module_list(entries: list) -> CheckResult:
    """The non-vacuity guard: something is under test, demo included."""
    if not entries:
        return CheckResult(["no modules under test"], 0)
    problems = []
    if "conformance_demo" not in [cls.id for cls, _ in entries]:
        problems.append("conformance_demo is not under test")
    return CheckResult(problems, len(entries))


def _command(module_cls: type, command_name: str):
    for command in module_cls.commands:
        if command.name == command_name:
            return command
    raise ValueError(f"unknown command: {command_name!r}")


def _field_segments(path: str) -> list:
    """``data.sensors[].id`` to ``[(sensors, True), (id, False)]``."""
    segments = []
    for part in path.split(".")[1:]:
        if part.endswith("[]"):
            segments.append((part[:-2], True))
        else:
            segments.append((part, False))
    return segments


def _build_tree(fields: list) -> dict:
    """Nest field declarations into ``{kids: {(name, is_list): node}}``."""
    root: dict = {"kids": {}, "leaf": None}
    for item in fields:
        node = root
        for name, is_list in _field_segments(item.path):
            key = (name, is_list)
            if key not in node["kids"]:
                node["kids"][key] = {"kids": {}, "leaf": None}
            node = node["kids"][key]
        node["leaf"] = item
    return root


def _check_keys(node: dict, value: Any, path: str, problems: list) -> int:
    """Count declared keys under ``value``; flag undeclared ones.

    Missing keys are fine (fields may be absent); nulls are fine. Every
    key present in a mapping must match a declared plain or list child,
    and list items that are mappings must match the item node's children.
    """
    if isinstance(value, dict):
        count = 0
        for key, sub in value.items():
            plain = node["kids"].get((key, False))
            listed = node["kids"].get((key, True))
            if plain is None and listed is None:
                problems.append(f"undeclared path {path}.{key}")
                continue
            count += 1
            if isinstance(sub, (list, tuple)) and listed is not None:
                kid = listed
            else:
                kid = plain if plain is not None else listed
            count += _check_keys(kid, sub, f"{path}.{key}", problems)
        return count
    if isinstance(value, (list, tuple)):
        count = 0
        for item in value:
            count += _check_keys(node, item, path + "[]", problems)
        return count
    return 0


def _has_mac(text: str) -> bool:
    return _MAC_RE.search(text) is not None


def _has_ip(text: str) -> bool:
    for match in _IPV4_RE.finditer(text):
        if all(int(part) <= 255 for part in match.group(0).split(".")):
            return True
    for match in _IPV6_RE.finditer(text):
        candidate = match.group(0).strip(":")
        if candidate and "::" in match.group(0):
            return True
        if candidate.count(":") >= 7:
            return True
    return False


def _request_allowed(allow: list, method: str, target: str) -> bool:
    """Re-check one request against the allow-list, independently of core.

    Mirrors the core rule: normalised absolute path, ``fullmatch`` on the
    path part, ``%``/``..``/fragments always refused, query only when the
    entry sets ``allow_query``.
    """
    if not isinstance(target, str) or not target.startswith("/"):
        return False
    if "#" in target or "%" in target:
        return False
    if re.search(r"[\x00-\x20\x7f]", target):
        return False
    path, sep, query = target.partition("?")
    segments = path.split("/")[1:]
    if any(part in ("", ".", "..") for part in segments):
        return False
    for entry in allow:
        if entry.method != method.upper():
            continue
        if sep and not entry.allow_query:
            continue
        if re.fullmatch(entry.pattern, path):
            return True
    return False


# -- check 1: names ----------------------------------------------------------


def check_names(module_cls: type) -> CheckResult:
    """Module and full command names are valid, short, and unreserved."""
    problems = []
    examined = 0
    if _NAME_RE.fullmatch(module_cls.id) is None:
        problems.append(f"invalid module id: {module_cls.id!r}")
    examined += 1
    for command in module_cls.commands:
        examined += 1
        full = f"housesitter.{module_cls.id}.{command.name}"
        if _NAME_RE.fullmatch(command.name) is None:
            problems.append(f"invalid command name: {command.name!r}")
        if _FULL_NAME_RE.fullmatch(full) is None:
            problems.append(f"invalid full command name: {full!r}")
        if len(full) > 64:
            problems.append(f"full command name over 64 chars: {full!r}")
        if full.startswith(_RESERVED_PREFIXES):
            problems.append(f"full command name uses a reserved prefix: {full!r}")
    return CheckResult(problems, examined)


# -- check 2: drop-ins -------------------------------------------------------


def check_dropins(registry: Any) -> CheckResult:
    """Generated drop-ins pass the SDK rules, re-validated independently."""
    problems = []
    try:
        files = dropins.generate(registry, "/opt/housesitter/bin/housesitter")
    except Exception as exc:
        return CheckResult([f"drop-in generation failed: {exc}"], 0)
    examined = 0
    for filename, content in sorted(files.items()):
        examined += 1
        name = filename[:-5] if filename.endswith(".json") else filename
        if not filename.endswith(".json"):
            problems.append(f"drop-in file without .json suffix: {filename!r}")
        if _FULL_NAME_RE.fullmatch(name) is None:
            problems.append(f"invalid drop-in command name: {name!r}")
        if len(name) > 64:
            problems.append(f"drop-in command name over 64 chars: {name!r}")
        if name.startswith(_RESERVED_PREFIXES):
            problems.append(f"drop-in name uses a reserved prefix: {name!r}")
        if not isinstance(content, dict):
            problems.append(f"drop-in {name!r} must hold a JSON object")
            continue
        for key in content:
            if key not in _DROPIN_KEYS:
                problems.append(f"drop-in {name!r} holds unknown key {key!r}")
        description = content.get("description")
        if not isinstance(description, str) or not description.strip():
            problems.append(f"drop-in {name!r} needs a non-blank description")
        exec_entry = content.get("exec")
        if (
            not isinstance(exec_entry, list)
            or len(exec_entry) != 3
            or any(not isinstance(item, str) or not item for item in exec_entry)
        ):
            problems.append(f"drop-in {name!r} exec must be 3 non-empty strings")
        else:
            binary, verb, full = exec_entry
            if not binary.startswith("/"):
                problems.append(f"drop-in {name!r} exec must start with an absolute path")
            if verb != "invoke" or full != name:
                problems.append(f"drop-in {name!r} exec must be [path, invoke, name]")
        for label in ("required", "optional"):
            mapping = content.get(label, {})
            if not isinstance(mapping, dict):
                problems.append(f"drop-in {name!r} {label} must be an object")
                continue
            for param_name, spec in mapping.items():
                if _NAME_RE.fullmatch(param_name) is None:
                    problems.append(
                        f"drop-in {name!r} has an invalid {label} param "
                        f"name: {param_name!r}"
                    )
                if not isinstance(spec, dict) or set(spec) != {"type", "description"}:
                    problems.append(
                        f"drop-in {name!r} param {param_name!r} must hold "
                        "exactly type and description"
                    )
                    continue
                if spec["type"] not in _PARAM_TYPES:
                    problems.append(
                        f"drop-in {name!r} param {param_name!r} has an "
                        "unknown type"
                    )
                if (
                    not isinstance(spec["description"], str)
                    or not spec["description"].strip()
                ):
                    problems.append(
                        f"drop-in {name!r} param {param_name!r} needs a "
                        "description"
                    )
        required = content.get("required", {})
        optional = content.get("optional", {})
        if isinstance(required, dict) and isinstance(optional, dict):
            overlap = set(required) & set(optional)
            if overlap:
                problems.append(
                    f"drop-in {name!r} repeats a param in required and "
                    f"optional: {sorted(overlap)}"
                )
        if "timeout_ms" in content:
            timeout = content["timeout_ms"]
            if (
                not isinstance(timeout, int)
                or isinstance(timeout, bool)
                or not 1 <= timeout <= 600_000
            ):
                problems.append(f"drop-in {name!r} timeout_ms must be 1..600000")
        size = len(
            (
                json.dumps(
                    content,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        )
        if size > MAX_DROPIN_BYTES:
            problems.append(f"drop-in {name!r} is over 64 KiB")
    return CheckResult(problems, examined)


# -- check 3: envelope -------------------------------------------------------


def check_stdio(run: Any) -> CheckResult:
    """The handler wrote nothing to stdout/stderr outside the log stream.

    The runner's own log stream is separate, so anything captured off the
    real ``sys.stdout``/``sys.stderr`` during a run is the module's: stray
    stdout would corrupt the SDK's JSON framing. The file-descriptor
    capture alongside catches writes that bypass the swapped objects
    (``os.write(1/2, ...)``, ``sys.__stdout__``/``sys.__stderr__``, and
    stdout/stderr references saved earlier). Threads that outlive the run
    and output at import time are outside both captures.
    """
    problems = []
    if run.stdout_captured:
        problems.append(f"{run.full_name}: handler wrote to stdout")
    if run.stderr_captured:
        problems.append(f"{run.full_name}: handler wrote to stderr")
    if getattr(run, "fd_stdout_captured", b""):
        problems.append(f"{run.full_name}: handler wrote to stdout (fd 1)")
    if getattr(run, "fd_stderr_captured", b""):
        problems.append(f"{run.full_name}: handler wrote to stderr (fd 2)")
    return CheckResult(problems, 4)


def module_has_lists(module_cls: type) -> bool:
    """True when the module declares any list under ``data``.

    Either an item-wise list field (a ``[]`` path) or an opaque array
    field (``Field(type="array")``) means the output has an unbounded
    part that must truncate under the large and hostile fixtures.
    """
    for command in module_cls.commands:
        for item in command.fields:
            if not item.path.startswith("data."):
                continue
            if "[]" in item.path or item.type == "array":
                return True
    return False


def check_envelope(run: Any) -> CheckResult:
    """The envelope shape is valid and the outcome matches the case expect."""
    problems = []
    env = run.outcome.envelope
    module_id = run.module_id
    case = run.case
    expect: Any = case["expect"] if case is not None else "page"
    if expect == "ok" or expect == "page":
        if run.outcome.exit_code != 0 or env.get("ok") is not True:
            problems.append(f"{run.full_name}: expected ok, got an error outcome")
            return CheckResult(problems, 1)
        problems.extend(_check_ok_shape(run, env))
        if (
            expect == "ok"
            and case is not None
            and case.get("fixture") == "empty"
        ):
            # An ``empty`` fixture that expects ok holds no data: every
            # top-level list in the envelope's data must be empty.
            data = env.get("data", {})
            if isinstance(data, dict):
                for key, value in data.items():
                    if isinstance(value, (list, tuple)) and len(value) > 0:
                        problems.append(
                            f"{run.full_name}: empty fixture returned "
                            f"a non-empty list {key!r}"
                        )
    elif expect == "truncated":
        if run.outcome.exit_code != 0 or env.get("ok") is not True:
            problems.append(f"{run.full_name}: expected truncated ok, got error")
            return CheckResult(problems, 1)
        problems.extend(_check_ok_shape(run, env))
        if env.get("truncated") is not True:
            problems.append(f"{run.full_name}: expected truncated=true")
        if not isinstance(env.get("more"), dict):
            problems.append(f"{run.full_name}: expected a more descriptor")
    elif isinstance(expect, dict):
        code = expect["error"]
        if run.outcome.exit_code == 0 or env.get("ok") is not False:
            problems.append(f"{run.full_name}: expected error {code}, got ok")
            return CheckResult(problems, 1)
        error = env.get("error")
        if not isinstance(error, dict):
            problems.append(f"{run.full_name}: error envelope holds no error object")
        else:
            if error.get("code") != code:
                problems.append(
                    f"{run.full_name}: expected error {code}, "
                    f"got {error.get('code')}"
                )
            if error.get("code") not in envelope.ERROR_CODES:
                problems.append(f"{run.full_name}: unknown error code")
            if not isinstance(error.get("message"), str) or not error.get("message"):
                problems.append(f"{run.full_name}: error message is empty")
        line = run.stderr_line or ""
        prefix = f"housesitter: {module_id}: {code}: "
        if not line.startswith(prefix):
            problems.append(f"{run.full_name}: stderr line has the wrong form")
        if len(line) >= 400:
            problems.append(f"{run.full_name}: stderr line is 400+ chars")
        if "\n" in line:
            problems.append(f"{run.full_name}: stderr line holds a newline")
    else:  # pragma: no cover - manifests are validated before checks run
        problems.append(f"{run.full_name}: invalid expect {expect!r}")
    return CheckResult(problems, 1)


def _check_ok_shape(run: Any, env: dict) -> list:
    problems = []
    module_id = run.module_id
    command = _command(run.module_cls, run.command_name)
    allowed = {
        "ok", "schema", "module", "module_version", "housesitter_version",
        "collected_at", "source", "data", "warnings", "truncated", "saved",
        "more", "label",
    }
    for key in env:
        if key not in allowed:
            problems.append(f"{run.full_name}: envelope holds unknown key {key!r}")
    expected_schema = (
        f"housesitter.{module_id}.{run.command_name}/{command.schema_version}"
    )
    if env.get("schema") != expected_schema:
        problems.append(f"{run.full_name}: schema is {env.get('schema')!r}")
    if env.get("module") != module_id:
        problems.append(f"{run.full_name}: module is {env.get('module')!r}")
    if env.get("module_version") != run.module_cls.version:
        problems.append(f"{run.full_name}: module_version mismatch")
    if env.get("housesitter_version") != housesitter.__version__:
        problems.append(f"{run.full_name}: housesitter_version mismatch")
    collected = env.get("collected_at")
    if not isinstance(collected, str) or _COLLECTED_RE.fullmatch(collected) is None:
        problems.append(f"{run.full_name}: collected_at has the wrong form")
    if not isinstance(env.get("source"), dict):
        problems.append(f"{run.full_name}: source must be an object")
    if not isinstance(env.get("data"), dict):
        problems.append(f"{run.full_name}: data must be an object")
    warnings = env.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(item, str) for item in warnings
    ):
        problems.append(f"{run.full_name}: warnings must be a list of strings")
    if not isinstance(env.get("truncated"), bool):
        problems.append(f"{run.full_name}: truncated must be a boolean")
    if env.get("saved") is not None and not isinstance(env.get("saved"), str):
        problems.append(f"{run.full_name}: saved must be a string or null")
    if env.get("more") is not None and not isinstance(env.get("more"), dict):
        problems.append(f"{run.full_name}: more must be an object or null")
    if "label" in env and env["label"] is not None and not isinstance(
        env["label"], str
    ):
        problems.append(f"{run.full_name}: label must be a string or null")
    return problems


# -- check 4: size and paging ------------------------------------------------


def check_size_paging(first: Any, follows: list, registry: Any) -> CheckResult:
    """Stdout fits the SDK budget on every page; ``more`` replays the rest."""
    problems = []
    examined = 0
    runs = [first] + list(follows)
    for run in runs:
        examined += 1
        if len(json.dumps(run.stdout_text)) - 2 > BUDGET_BYTES:
            problems.append(f"{run.full_name}: envelope exceeds 88 KiB")
    env = first.outcome.envelope
    if env.get("ok") is not True or env.get("truncated") is not True:
        return CheckResult(problems, examined)
    more = env.get("more")
    if not isinstance(more, dict):
        problems.append(f"{first.full_name}: truncated result needs more")
        return CheckResult(problems, examined)
    if not first.raw_results:
        problems.append(f"{first.full_name}: no recorded Result for paging")
        return CheckResult(problems, examined)
    paging = first.raw_results[0].paging
    if paging is None:
        problems.append(f"{first.full_name}: truncated result has no Paging")
        return CheckResult(problems, examined)
    try:
        registry.get_command(more.get("command"))
    except KeyError:
        problems.append(
            f"{first.full_name}: more.command {more.get('command')!r} "
            "is not registered"
        )
        return CheckResult(problems, examined)
    examined += 1
    params = more.get("params")
    if not isinstance(params, dict):
        problems.append(f"{first.full_name}: more.params must be an object")
        return CheckResult(problems, examined)
    if params.get("section") not in list(paging.lists):
        problems.append(f"{first.full_name}: more.params section is not paged")
    offset = params.get("offset")
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
    ):
        problems.append(f"{first.full_name}: more.params offset is invalid")
    saved = env.get("saved")
    raw_id = dict(paging.params).get("id")
    more_id = params.get("id")
    if not isinstance(more_id, str) or not more_id:
        problems.append(f"{first.full_name}: more.params id is missing")
    elif _STORE_ID_RE.fullmatch(more_id) is None:
        # ``id`` is exempt from the warnings/params text check, so it must
        # be a store id, never module-chosen text (review finding 7).
        problems.append(f"{first.full_name}: more.params id is not a store id")
    elif saved is not None and more_id != saved:
        problems.append(f"{first.full_name}: more.params id is not the saved id")
    elif saved is None and raw_id is not None and more_id != raw_id:
        problems.append(f"{first.full_name}: more.params id is not the paging id")
    remaining = more.get("remaining")
    if not isinstance(remaining, dict) or not remaining:
        problems.append(f"{first.full_name}: more.remaining must be a non-empty object")
    else:
        for section, cut in remaining.items():
            examined += 1
            if section not in list(paging.lists):
                problems.append(
                    f"{first.full_name}: remaining section {section!r} "
                    "is not paged"
                )
            if (
                not isinstance(cut, int) or isinstance(cut, bool) or cut < 1
            ):
                problems.append(
                    f"{first.full_name}: remaining {section!r} must be >= 1"
                )
    saved_obj = first.saved_obj
    if saved_obj is None and isinstance(raw_id, str):
        # A paging command may page its own output without saving again,
        # carrying the existing id in Paging.params (ARCHITECTURE §5).
        path = Path(first.data_dir) / f"{raw_id}.json"
        if _STORE_ID_RE.fullmatch(raw_id) and path.is_file():
            saved_obj = json.loads(path.read_text(encoding="utf-8"))
    if saved_obj is None:
        problems.append(f"{first.full_name}: truncated result saved nothing")
        return CheckResult(problems, examined)
    saved_data = saved_obj.get("data", {})

    # Every follow-up must itself be a good page.
    for run in follows:
        examined += 1
        if run.outcome.exit_code != 0 or run.outcome.envelope.get("ok") is not True:
            problems.append(f"{run.full_name}: follow-up page is not ok")
    # The chain must terminate: the last page has ``more`` null. A chain
    # that is still truncated at the harness's 50-follow cap fails here.
    if runs[-1].outcome.envelope.get("more") is not None:
        problems.append(
            f"{first.full_name}: paging did not terminate "
            "(the last page still has more)"
        )

    def _paging_of(run: Any) -> Any:
        if run.raw_results:
            return run.raw_results[0].paging
        return None

    # Each follow-up replays the previous page's ``more`` exactly, its
    # offset advances by the items the previous page kept, and its
    # remaining count equals the true count left.
    for prev, cur in zip(runs[:-1], runs[1:]):
        examined += 1
        prev_more = prev.outcome.envelope.get("more")
        if not isinstance(prev_more, dict):
            problems.append(
                f"{prev.full_name}: the paging chain continues "
                "after a terminal page"
            )
            continue
        prev_params = prev_more.get("params")
        if not isinstance(prev_params, dict):
            continue
        if cur.params != prev_params:
            problems.append(
                f"{cur.full_name}: follow-up params do not match "
                "the previous more.params"
            )
        section = prev_params.get("section")
        offset = prev_params.get("offset")
        prev_paging = _paging_of(prev)
        if (
            isinstance(section, str)
            and isinstance(offset, int)
            and not isinstance(offset, bool)
            and prev_paging is not None
            and prev_paging.params is not None
        ):
            start = dict(prev_paging.start).get(section, 0)
            kept_page = prev.outcome.envelope.get("data", {}).get(section, [])
            if isinstance(kept_page, (list, tuple)):
                if offset != start + len(kept_page):
                    problems.append(
                        f"{prev.full_name}: more.params offset {offset} "
                        "is not start + items kept"
                    )
                saved_list = saved_data.get(section, [])
                if isinstance(saved_list, (list, tuple)):
                    remaining_map = prev_more.get("remaining")
                    remaining = (
                        remaining_map.get(section)
                        if isinstance(remaining_map, dict)
                        else None
                    )
                    if remaining != len(saved_list) - offset:
                        problems.append(
                            f"{prev.full_name}: more.remaining for "
                            f"{section!r} is not the true count left"
                        )

    for section in paging.lists:
        examined += 1
        expected = saved_data.get(section, [])
        # A page that starts mid-list (Paging.start) accounts only for the
        # items from its start onwards.
        start = dict(paging.start).get(section, 0)
        if isinstance(expected, (list, tuple)):
            expected = list(expected)[start:]
        collected: list = []
        for run in runs:
            page = run.outcome.envelope.get("data", {}).get(section, [])
            if isinstance(page, (list, tuple)):
                collected.extend(page)
        if not isinstance(expected, list) or len(collected) != len(expected):
            problems.append(
                f"{first.full_name}: paging section {section!r} lost items "
                "or returned duplicates"
            )
            continue
        # Positional comparison under the effective projection: each
        # returned item is compared with the saved item at the same
        # position, projected onto the returned item's keys. Identifier
        # removal (``identifiers: none``) and compact forms that drop keys
        # (such as the item id) only remove keys, so the projected saved
        # item must equal the returned one. Items are compared in page
        # order from the section's start offset, preserving duplicates.
        for item, saved_item in zip(collected, expected):
            if isinstance(item, dict) and isinstance(saved_item, dict):
                projected = {
                    key: saved_item[key] for key in item if key in saved_item
                }
                if set(projected) != set(item) or any(
                    projected[key] != item[key] for key in item
                ):
                    problems.append(
                        f"{first.full_name}: paging section {section!r} "
                        "item differs from the saved copy"
                    )
                    break
            elif item != saved_item:
                problems.append(
                    f"{first.full_name}: paging section {section!r} "
                    "item differs from the saved copy"
                )
                break
    return CheckResult(problems, examined)


# -- check 5: HTTP allow-list -------------------------------------------------


def check_http(module_cls: type, session: Any, runs: list) -> CheckResult:
    """Recorded requests match the allow-list; probes prove the denials."""
    problems = []
    examined = 0
    allow = list(module_cls.http_allow)
    for run in runs:
        examined += 1
        if run.outcome.error_code == "not_allowed":
            name = run.case["name"] if run.case else run.full_name
            problems.append(f"{name}: handler attempted off-allow-list HTTP")
    for run in runs:
        for request in run.requests:
            examined += 1
            if not _request_allowed(allow, request.method, request.target):
                problems.append(
                    f"recorded request {request.method} {request.target} "
                    "is not allow-listed"
                )
    from harness import config_implied_binding

    for run in runs:
        if not run.factory_calls:
            continue
        implied = config_implied_binding(run.config_used)
        for call in run.factory_calls:
            examined += 1
            if (call["scheme"], call["host"], call["port"]) != implied:
                problems.append(
                    f"client binding {call} does not match the config {implied}"
                )
    get_paths = sorted(
        {
            request.target
            for run in runs
            for request in run.requests
            if request.method == "GET"
        }
    )
    if not get_paths:
        # A module that never reads cannot have its denials probed
        # (re-review finding 4); a read-only module always reads.
        problems.append("no GET request recorded; allow-list probes did not run")
        return CheckResult(problems, examined)
    denied, count = _probe_denials(module_cls, session, allow, get_paths)
    problems.extend(denied)
    examined += count
    problems.extend(_probe_redirect(module_cls, session, get_paths[0]))
    examined += 1
    return CheckResult(problems, examined)


def _probe_denials(
    module_cls: type, session: Any, allow: list, get_paths: list
) -> tuple:
    """Write methods, traversal tricks, and unknown paths must raise
    ``NotAllowed`` without opening a new connection."""
    from core.helpers import ScriptedServer, canned_handler

    problems = []
    count = 0
    server = ScriptedServer(canned_handler({}))
    try:
        ctx = session.context_for(server.port)
        base = f"http://127.0.0.1:{server.port}"
        before = server.connections

        def attempt(method: str, target: str) -> None:
            nonlocal count
            count += 1
            try:
                with ctx.http(base) as live:
                    live.request(method, target)
            except http_core.NotAllowed:
                return
            except http_core.UpstreamError as exc:
                problems.append(
                    f"{method} {target}: wrong error {exc.code}, "
                    "expected NotAllowed"
                )
            else:
                problems.append(f"{method} {target}: was not refused")

        for path in get_paths:
            # A recorded target may carry a query string: split it off and
            # build every probe from the path part, so traversal and query
            # probes stay valid. Whether ``?q`` is allowed is decided per
            # matching entry (one that fullmatches the path part).
            base_path = path.partition("?")[0]
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                if _request_allowed(allow, method, base_path):
                    continue
                attempt(method, base_path)
            attempt("GET", base_path + "/../x")
            attempt("GET", base_path + "/%2e%2e/x")
            query_allowed = any(
                entry.allow_query
                and entry.method == "GET"
                and re.fullmatch(entry.pattern, base_path)
                for entry in allow
            )
            if not query_allowed:
                attempt("GET", base_path + "?q=1")
            attempt("GET", "/not-allowed-example")
        if server.connections != before:
            problems.append("a refused probe opened a new connection")
    finally:
        server.close()
    return problems, count


def _probe_redirect(module_cls: type, session: Any, path: str) -> list:
    """A 302 on a recorded GET path raises instead of following."""
    from core.helpers import Canned, ScriptedServer, canned_handler

    problems = []
    base_path = path.partition("?")[0]
    server = ScriptedServer(
        canned_handler(
            {
                (("GET", base_path)): Canned(
                    status=302, headers={"Location": "/go"}, body=b""
                )
            }
        )
    )
    try:
        ctx = session.context_for(server.port)
        base = f"http://127.0.0.1:{server.port}"
        try:
            with ctx.http(base) as live:
                live.get(base_path)
        except http_core.UpstreamError as exc:
            if exc.code != "upstream_unexpected":
                problems.append(f"redirect raised {exc.code}, not upstream_unexpected")
        except http_core.NotAllowed:
            problems.append("redirect probe path was not allow-listed")
        else:
            problems.append("a 302 redirect was not raised as an error")
        if len(server.requests) != 1:
            problems.append(
                f"redirect probe sent {len(server.requests)} requests, "
                "expected exactly 1"
            )
    finally:
        server.close()
    return problems


# -- check 6: hostile names ---------------------------------------------------


def check_hostile_names(run: Any, require_dirty: bool = True) -> CheckResult:
    """Declared untrusted envelope strings are sanitised; the raw ones were not."""
    problems = []
    examined = 0
    command = _command(run.module_cls, run.command_name)

    def _check_value(value: str, leaf: Any, shown: str) -> None:
        nonlocal examined
        examined += 1
        if len(value) > leaf.max_len:
            problems.append(f"{shown} is longer than {leaf.max_len}")
        if value != value.strip() or re.search(r"\s{2,}", value):
            problems.append(f"{shown} holds a whitespace run or edge space")
        if any(
            unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value
        ):
            problems.append(f"{shown} holds control/format characters")

    def _walk(fields_tree: dict, value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, sub in value.items():
                plain = fields_tree["kids"].get((key, False))
                listed = fields_tree["kids"].get((key, True))
                kid = plain if plain is not None else listed
                if kid is None:
                    continue
                leaf = kid["leaf"]
                if (
                    leaf is not None
                    and leaf.untrusted
                    and leaf.type == "string"
                ):
                    if isinstance(sub, str):
                        _check_value(sub, leaf, f"{path}.{key}")
                    elif isinstance(sub, (list, tuple)):
                        for item in sub:
                            if isinstance(item, str):
                                _check_value(item, leaf, f"{path}.{key}")
                if isinstance(sub, dict):
                    _walk(kid, sub, f"{path}.{key}")
                elif isinstance(sub, (list, tuple)) and listed is not None:
                    for item in sub:
                        if isinstance(item, dict):
                            _walk(listed, item, f"{path}.{key}[]")
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(fields_tree, item, path + "[]")

    data_tree = _build_tree(
        [item for item in command.fields if item.path.startswith("data.")]
    )
    source_tree = _build_tree(
        [item for item in command.fields if item.path.startswith("source.")]
    )
    env = run.outcome.envelope
    if env.get("ok") is True:
        _walk(data_tree, env.get("data", {}), "data")
        _walk(source_tree, env.get("source", {}), "source")
    dirty = 0
    for result in run.raw_results:
        dirty += _count_dirty(data_tree, result.data)
        dirty += _count_dirty(source_tree, result.source)
    if require_dirty and not dirty:
        problems.append(
            f"{run.full_name}: no raw untrusted value was dirty or over-long; "
            "the hostile check is vacuous"
        )
    return CheckResult(problems, examined)


def _is_dirty_text(value: str, max_len: int) -> bool:
    """True when sanitising would change this raw untrusted string."""
    return (
        len(value) > max_len
        or value != value.strip()
        or re.search(r"\s{2,}", value) is not None
        or any(
            unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value
        )
    )


def _count_dirty(node: dict, value: Any) -> int:
    """Count raw untrusted strings that sanitising would change."""
    found = 0
    if isinstance(value, dict):
        for key, sub in value.items():
            plain = node["kids"].get((key, False))
            listed = node["kids"].get((key, True))
            if plain is None and listed is None:
                continue
            if isinstance(sub, (list, tuple)) and listed is not None:
                kid = listed
            else:
                kid = plain if plain is not None else listed
            leaf = kid["leaf"]
            if leaf is not None and leaf.untrusted and leaf.type == "string":
                # A plain string, or a declared list of strings (``x[]``),
                # mirroring the envelope walker (round-3 finding 1).
                texts = [sub] if isinstance(sub, str) else (
                    sub if isinstance(sub, (list, tuple)) else []
                )
                found += sum(
                    1
                    for text in texts
                    if isinstance(text, str) and _is_dirty_text(text, leaf.max_len)
                )
            found += _count_dirty(kid, sub)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found += _count_dirty(node, item)
    return found


# -- check 7: planted secrets -------------------------------------------------


def check_secrets(run: Any, planted: set) -> CheckResult:
    """No planted secret appears in output, saved files, logs, or errors."""
    problems = []
    examined = 0
    haystacks = [("stdout", run.stdout_text)]
    for path in sorted(run.data_dir.rglob("*")):
        if path.is_file():
            try:
                haystacks.append((str(path), path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
    # Stray writes captured off the real sys.stdout/sys.stderr during the
    # run: a handler must not print, and both streams are scanned. The
    # file-descriptor captures alongside hold the same run's fd-level
    # writes (os.write, sys.__stdout__, saved references).
    haystacks.append(("captured stdout", run.stdout_captured))
    haystacks.append(("captured stderr", run.stderr_captured))
    for label, blobs in (
        ("captured fd stdout", (getattr(run, "fd_stdout_captured", b""),)),
        ("captured fd stderr", (getattr(run, "fd_stderr_captured", b""),)),
    ):
        for blob in blobs:
            if isinstance(blob, (bytes, bytearray)):
                haystacks.append((label, bytes(blob).decode("utf-8", "replace")))
            elif blob:
                haystacks.append((label, str(blob)))
    # Every payload saved at write time, before retention can prune it.
    for payload in run.saved_history:
        haystacks.append(("saved payload", payload))
    haystacks.append(("log stream", run.log_stream_text))
    haystacks.append(("log records", "\n".join(run.log_records)))
    if run.outcome.exit_code != 0:
        haystacks.append(
            ("error envelope", envelope.serialize(run.outcome.envelope))
        )
        haystacks.append(("stderr line", run.stderr_line or ""))
    for label, text in haystacks:
        examined += 1
        for secret in planted:
            if secret and secret in text:
                problems.append(f"planted secret in {label}")
                break
    return CheckResult(problems, examined)


# -- check 8: declared fields -------------------------------------------------


def check_fields(run: Any) -> CheckResult:
    """Every envelope path is declared; RAW results carry no undeclared keys."""
    problems = []
    examined = 0
    command = _command(run.module_cls, run.command_name)
    data_tree = _build_tree(
        [item for item in command.fields if item.path.startswith("data.")]
    )
    source_tree = _build_tree(
        [item for item in command.fields if item.path.startswith("source.")]
    )
    env = run.outcome.envelope
    if env.get("ok") is True:
        examined += _check_keys(data_tree, env.get("data", {}), "data", problems)
        examined += _check_keys(
            source_tree, env.get("source", {}), "source", problems
        )
    for result in run.raw_results:
        examined += _check_keys(
            data_tree, dict(result.data), "data(raw)", problems
        )
        examined += _check_keys(
            source_tree, dict(result.source), "source(raw)", problems
        )
        if result.compact is not None:
            examined += _check_keys(
                data_tree, dict(result.compact), "data.compact(raw)", problems
            )
    return CheckResult(problems, examined)


# -- check 9: spec ------------------------------------------------------------


def parse_spec_tables(spec_path: Path) -> dict:
    """Parse MODULES.md §4 field tables: lone backticked path, then rows.

    Tables inside fenced code blocks are ignored, and a duplicated
    object table is an error (MODULES §4 wants one table per object).
    """
    try:
        lines = Path(spec_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ValueError(f"cannot read spec: {spec_path}") from None
    tables: dict[str, list[str]] = {}
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence:
            index += 1
            continue
        match = re.fullmatch(r"`([^`]+)`", line.strip())
        index += 1
        if not match:
            continue
        while index < len(lines) and not lines[index].strip():
            index += 1
        block = []
        while index < len(lines) and lines[index].strip().startswith("|"):
            block.append(lines[index].strip())
            index += 1
        if len(block) < 3:
            continue
        rows = []
        for line in block[2:]:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and cells[0]:
                rows.append(cells[0].strip("` "))
        if match.group(1) in tables:
            raise ValueError(
                f"duplicate spec table for object {match.group(1)!r}: "
                f"{spec_path}"
            )
        tables[match.group(1)] = rows
    return tables


def _spec_key(field_path: str) -> tuple:
    parts = field_path.split(".")
    last = parts[-1]
    if last.endswith("[]") and len(parts) > 2:
        return (".".join(parts[:-1]), last[:-2])
    return (".".join(parts[:-1]), last)


def check_spec(module_cls: type, spec_path: Path) -> CheckResult:
    """Declared fields and spec rows match in both directions."""
    try:
        tables = parse_spec_tables(spec_path)
    except ValueError as exc:
        return CheckResult([str(exc)], 0)
    declared = set()
    for command in module_cls.commands:
        for item in command.fields:
            declared.add(_spec_key(item.path))
    spec_rows = set()
    for table, rows in tables.items():
        for row in rows:
            spec_rows.add((table, row))
    problems = []
    for table, row in sorted(declared - spec_rows):
        problems.append(f"declared field {table}.{row} is missing from the spec")
    for table, row in sorted(spec_rows - declared):
        problems.append(f"spec row {table}.{row} is not a declared field")
    return CheckResult(problems, len(declared) + len(spec_rows))


# -- check 10: warnings and paging params -------------------------------------


def check_warnings_params(
    run: Any, upstream_leaves: set, planted: set | None = None
) -> CheckResult:
    """Warnings and replay params carry no identifiers, secrets, or data."""
    problems = []
    examined = 0
    env = run.outcome.envelope
    raw_values = set()
    for result in run.raw_results:
        command = _command(run.module_cls, run.command_name)
        for item in command.fields:
            if not (item.identifier or item.untrusted):
                continue
            raw_values.update(_values_at(result, item.path))

    def _check_text(text: str, shown: str) -> None:
        nonlocal examined
        examined += 1
        if _has_mac(text):
            problems.append(f"{shown} holds a MAC address")
        if _has_ip(text):
            problems.append(f"{shown} holds an IP address")
        if planted:
            for secret in planted:
                if secret and secret in text:
                    problems.append(f"{shown} holds a planted secret")
                    break
        for leaf in upstream_leaves:
            # A single lowercase word ("sensor", "temperature") can match
            # author-written text by coincidence (review finding 3); names,
            # identifiers, and mixed text are still checked.
            if len(leaf) < 6 or _PLAIN_WORD_RE.fullmatch(leaf):
                continue
            if leaf in text:
                problems.append(f"{shown} repeats upstream text")
                break
        for value in raw_values:
            # Whole-token match: a sensor named "on" must not match the
            # author's "online" (re-review finding 3).
            if value and re.search(
                r"(?<!\w)" + re.escape(value) + r"(?!\w)", text
            ):
                problems.append(f"{shown} repeats a raw identifier or name")
                break

    for warning in env.get("warnings", []):
        if isinstance(warning, str):
            _check_text(warning, "warning")
    more = env.get("more")
    if isinstance(more, dict):
        params = more.get("params", {})
        if isinstance(params, dict):
            for key, value in params.items():
                if key in ("id", "section", "offset"):
                    continue
                if isinstance(value, str):
                    _check_text(value, f"more.params {key}")
                else:
                    examined += 1
    return CheckResult(problems, examined)


def _values_at(result: Any, field_path: str) -> set:
    """Raw string values at one declared field path."""
    found: list[str] = []

    def visit(node: Any, segments: list) -> None:
        if not segments:
            if isinstance(node, str):
                found.append(node)
            return
        name, is_list = segments[0]
        rest = segments[1:]
        if not isinstance(node, dict) or name not in node:
            return
        child = node[name]
        # ``x[]`` marks the value at ``x`` as a list: walk its items.
        if is_list:
            if isinstance(child, (list, tuple)):
                for item in child:
                    visit(item, rest)
        else:
            visit(child, rest)

    segments = _field_segments(field_path)
    if field_path.startswith("data."):
        visit(dict(result.data), segments)
    else:
        visit(dict(result.source), segments)
    return set(found)


# -- check 11: skill ----------------------------------------------------------


_BLOCK_SCALARS = frozenset({">", ">-", ">+", "|", "|-", "|+"})


def _parse_skill_front_matter(head: list) -> tuple:
    """Parse the strict front-matter subset; return (fields, problems).

    ``name`` and ``description`` are scalars in one of three forms: a
    quoted scalar (single or double quotes, properly closed, non-empty
    content, which may hold anything), a ``>-``/``|`` block scalar with
    non-empty content, or a plain scalar. A plain scalar never holds an
    unquoted ``": "``, is never a YAML null/bool word (``null``, ``~``,
    ``true``, ``false``, ``yes``, ``no``), never starts with ``[``,
    ``{``, ``#``, ``&``, ``*``, ``!``, or a bare ``|``/``>``, and is
    non-blank after stripping comments. Anything else is a problem.
    """
    problems = []
    fields: dict[str, str] = {}
    index = 0
    while index < len(head):
        line = head[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if line[:1] in (" ", "\t"):
            problems.append(
                "SKILL.md front matter has an unexpected indented line"
            )
            index += 1
            continue
        if ":" not in line:
            problems.append(
                f"SKILL.md front matter line is not key: value: {stripped!r}"
            )
            index += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key not in ("name", "description"):
            problems.append(
                f"SKILL.md front matter holds unexpected key {key!r}"
            )
            index += 1
            continue
        if key in fields:
            problems.append(f"SKILL.md front matter repeats key {key!r}")
            index += 1
            continue
        if value in _BLOCK_SCALARS:
            chunk = []
            index += 1
            while index < len(head) and head[index][:1] in (" ", "\t"):
                chunk.append(head[index].strip())
                index += 1
            content = " ".join(part for part in chunk if part)
            if not content:
                problems.append(
                    f"SKILL.md front matter {key} block scalar is empty"
                )
            else:
                fields[key] = content
            continue
        content = value
        if content[:1] in ("'", '"'):
            # A quoted scalar stands on its own: it needs its closing
            # quote and non-empty content, but the text inside may hold
            # anything (even "[literal]" or "null").
            quote = content[0]
            if len(content) < 2 or not content.endswith(quote):
                problems.append(
                    f"SKILL.md front matter {key} quote never closes"
                )
                index += 1
                continue
            content = content[1:-1].strip()
            if not content:
                problems.append(f"SKILL.md front matter has no {key}")
            else:
                fields[key] = content
        else:
            content = re.sub(r"(^|\s)#.*$", "", content).strip()
            if not content:
                problems.append(f"SKILL.md front matter has no {key}")
            elif ": " in content:
                problems.append(
                    f"SKILL.md front matter {key} has an unquoted ': '"
                )
            elif content.lower() in (
                "null", "~", "true", "false", "yes", "no",
            ):
                problems.append(
                    f"SKILL.md front matter {key} must not be {content!r}"
                )
            elif content[0] in "[{&*!|>#":
                problems.append(
                    f"SKILL.md front matter {key} is not a plain scalar"
                )
            else:
                fields[key] = content
        index += 1
    return fields, problems


def check_skill(module_cls: type, planted: set | None = None) -> CheckResult:
    """SKILL.md exists, has a name and description, and covers each command."""
    problems = []
    text, warning = builtins._read_skill_text(module_cls, module_cls.id)
    if warning is not None or text is None:
        return CheckResult([f"SKILL.md unreadable: {warning}"], 0)
    lines = text.splitlines()
    # The front matter is the first lines between ``---`` fences.
    if not lines or lines[0].strip() != "---":
        return CheckResult(["SKILL.md has no YAML front matter"], 1)
    end = None
    for pos in range(1, len(lines)):
        if lines[pos].strip() == "---":
            end = pos
            break
    if end is None:
        return CheckResult(["SKILL.md front matter never closes"], 1)
    head = lines[1:end]
    body = "\n".join(lines[end + 1 :])
    fields, problems = _parse_skill_front_matter(head)
    if "name" not in fields and not any("name" in p for p in problems):
        problems.append("SKILL.md front matter has no name")
    if "description" not in fields and not any("description" in p for p in problems):
        problems.append("SKILL.md front matter has no description")
    # SKILL.md is returned verbatim by ``describe``: a planted value
    # smuggled into it reaches Muse. The marker alone is the signal, so
    # this holds with or without the planted corpus.
    examined_extra = 1
    if "PLANTED-SECRET" in text:
        problems.append("planted secret in SKILL.md")
    elif planted:
        for secret in planted:
            if secret and secret in text:
                problems.append("planted secret in SKILL.md")
                break
    examined = 1 + examined_extra
    for command in module_cls.commands:
        examined += 1
        full = f"housesitter.{module_cls.id}.{command.name}"
        if full not in body:
            problems.append(f"SKILL.md never mentions {full}")
    return CheckResult(problems, examined)


# -- check 12: evidence scenarios ----------------------------------------------


def check_evidence(registry: Any, scenarios: list) -> CheckResult:
    """Every registered command has an evidence scenario invoking it."""
    problems = []
    examined = 0
    for full_name in sorted(registry.commands):
        examined += 1
        hit = any(
            isinstance(item, dict)
            and list(item.get("argv", []))[:2] == ["invoke", full_name]
            for item in scenarios
        )
        if not hit:
            problems.append(f"command {full_name} has no evidence scenario")
    return CheckResult(problems, examined)
