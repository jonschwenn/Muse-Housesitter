"""Output envelope, stderr error lines, size budget, text sanitiser.

Implements ARCHITECTURE §4, §5, and §7:

- every command's stdout is one compact JSON object with a fixed key set;
- on failure the process exits non-zero and stderr's last line is
  ``housesitter: <module>: <code>: <reason>`` (under 400 characters);
- the serialised stdout must satisfy the SDK's own measure,
  ``len(json.dumps(text)) - 2 <= 88 KiB``;
- upstream-supplied strings are sanitised (Unicode ``Cc``/``Cf``
  stripped, whitespace collapsed, capped) and never used as JSON keys.

Seam (later PRs): ``core/store.py`` supplies ``saved`` ids and the
``more`` paging descriptor; ``core/cli.py`` prints the stderr line and
the ``{"ok": false, ...}`` stdout object.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# §5: 88 KiB by the SDK's measure, leaving headroom under its 96 KiB cut.
BUDGET_BYTES = 88 * 1024

# §4: stderr line stays under 400 characters.
MAX_ERROR_LINE = 400

# §7: untrusted text is capped at 64 characters; owner-supplied labels at 200.
MAX_TEXT_LEN = 64
MAX_LABEL_LEN = 200

# §4: the stable error codes. ``config_invalid`` is raised by config
# validation (a later PR); the rest are produced by this core.
ERROR_CODES = frozenset(
    {
        "config_invalid",
        "secret_unreadable",
        "upstream_auth",
        "upstream_unreachable",
        "upstream_tls",
        "upstream_unexpected",
        "not_allowed",
        "timeout",
        "bad_params",
        "not_found",
    }
)

_MODULE_RE = re.compile(r"[a-z][a-z0-9_]*")

_WS_RE = re.compile(r"\s+")


def sanitize_text(value: Any, max_len: int = MAX_TEXT_LEN) -> Any:
    """Sanitise one upstream-supplied string (§7, rule 8).

    Non-strings pass through unchanged. Otherwise: collapse runs of
    whitespace to single spaces (so newlines separate words instead of
    joining them), drop every character in Unicode categories ``Cc``,
    ``Cf``, and ``Cs`` (control, bidi overrides, zero-width characters,
    NUL, and lone surrogates), strip the ends, and cap at ``max_len``
    characters.

    The result is a *value* only: never use it as a JSON object key.
    Groupings that need a name use lists of ``{id, name, ...}`` objects.
    """
    if not isinstance(value, str):
        return value
    collapsed = _WS_RE.sub(" ", value)
    cleaned = "".join(
        char for char in collapsed if unicodedata.category(char) not in ("Cc", "Cf", "Cs")
    )
    return cleaned.strip()[:max_len]


def sanitize_label(value: Any) -> Any:
    """Sanitise owner-supplied free text (§7): same rules, 200 characters."""
    return sanitize_text(value, MAX_LABEL_LEN)


def format_error(module: str, code: str, reason: str) -> str:
    """Build stderr's last line: ``housesitter: <module>: <code>: <reason>``.

    The result is under 400 characters and holds no newlines: every run
    of whitespace or control characters in ``reason`` is collapsed to a
    single space first, so the line the SDK reads (its "last stderr
    line") always carries the whole ``<module>: <code>:`` prefix.
    ``reason`` must be a plain, author-written string: never upstream
    response text, and never secrets, URLs with credentials, bodies, or
    cookie values.
    """
    if not isinstance(module, str) or _MODULE_RE.fullmatch(module) is None:
        raise ValueError(f"invalid module name: {module!r}")
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")
    if not isinstance(reason, str):
        reason = str(reason)
    flattened = "".join(
        " " if (char.isspace() or unicodedata.category(char) == "Cc") else char
        for char in reason
    )
    reason = re.sub(r" {2,}", " ", flattened)
    line = f"housesitter: {module}: {code}: {reason}"
    if len(line) >= MAX_ERROR_LINE:
        line = line[: MAX_ERROR_LINE - 2] + "…"
    return line


def serialize(envelope: dict) -> str:
    """Serialise an envelope exactly as it goes to stdout (compact).

    Non-finite floats are never emitted: ``allow_nan=False`` makes them
    raise ``ValueError`` instead of writing a bare ``NaN`` token.
    """
    return json.dumps(
        envelope, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def measure(text: str) -> int:
    """The SDK's own output measure (§2.2): ``len(json.dumps(text)) - 2``."""
    return len(json.dumps(text)) - 2


def within_budget(text: str, budget: int = BUDGET_BYTES) -> bool:
    """True when the already-serialised stdout ``text`` fits the budget."""
    return measure(text) <= budget


def build_ok(
    *,
    schema: str,
    module: str,
    module_version: str,
    housesitter_version: str,
    collected_at: str,
    source: dict,
    data: dict,
    warnings: list | tuple = (),
    truncated: bool = False,
    saved: str | None = None,
    more: dict | None = None,
    label: str | None = None,
) -> dict:
    """Build a success envelope (§4). Keys are fixed; ``data`` is module-defined."""
    envelope: dict[str, Any] = {
        "ok": True,
        "schema": schema,
        "module": module,
        "module_version": module_version,
        "housesitter_version": housesitter_version,
        "collected_at": collected_at,
        "source": source,
        "data": data,
        "warnings": list(warnings),
        "truncated": truncated,
        "saved": saved,
        "more": more,
    }
    if label is not None:
        envelope["label"] = label
    return envelope


def build_error(code: str, message: str) -> dict:
    """Build the stdout object for the CLI path on failure (§4)."""
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")
    return {"ok": False, "error": {"code": code, "message": message}}
