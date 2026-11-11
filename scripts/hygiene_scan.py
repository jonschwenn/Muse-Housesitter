#!/usr/bin/env python3
"""Repo hygiene scanner: the single source of truth for "no real house data,
no likely secrets" (AGENTS.md, Testing rules).

Two callers, one code path:

- ``tests/test_hygiene.py`` scans the working tree through :func:`scan_bytes`.
- ``.github/workflows/ci-guard.yml`` runs the *base* copy of this file with
  ``python -I`` over the PR head tree's blobs (read through git plumbing, never
  checked out, never imported) through :func:`scan_git_tree`. That is what
  makes the scan tamper-proof: a PR can weaken its own pytest run, but it
  cannot change the scanner that runs in the guard, because the guard always
  runs this file from the base commit.

What is enforced must not change here without care: the same rules, the same
``hygiene: ok`` handling, and the same fixture-only rules as the test suite
pins. Stdlib only, Python 3.9+, no pytest import.

Patterns are assembled from parts so that this file does not match itself.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import time
from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath

HEX = "[0-9A-Fa-f]"
MAC_RES = [
    # 02:00:00:00:00:01 and 02-00-00-00-00-01
    re.compile(rf"(?<![0-9A-Fa-f:])(?:{HEX}{{2}}[:-]){{5}}{HEX}{{2}}(?![0-9A-Fa-f:])"),
    # 0200.0000.0001 (Cisco style)
    re.compile(rf"(?<![0-9A-Fa-f.])(?:{HEX}{{4}}\.){{2}}{HEX}{{4}}(?![0-9A-Fa-f.])"),
]
# 020000000001: bare, as some upstream APIs return it. Checked in fixtures only,
# because elsewhere 12 hex digits are usually a short git SHA or a counter.
BARE_MAC_RE = re.compile(rf"(?<![0-9A-Fa-f-])(?:{HEX}{{12}})(?![0-9A-Fa-f-])")
ALLOWED_MAC_PREFIXES = ("020000", "00005e0053")

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
# The lookahead excludes "." so an IPv4-mapped address (::ffff:192.0.2.1) is left
# to the IPv4 check instead of being cut at the dot.
IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:.])"
)
ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "192.0.2.0/24",  # RFC 5737 documentation ranges
        "198.51.100.0/24",
        "203.0.113.0/24",
        "127.0.0.0/8",  # loopback
        "0.0.0.0/32",
        "255.0.0.0/8",  # netmasks and broadcast, e.g. 255.255.255.0
        "2001:db8::/32",  # RFC 3849 IPv6 documentation range
        "::1/128",
        "ff00::/8",  # multicast, e.g. ff02::1
    )
]

# A line containing this marker is skipped, for the rare false positive (e.g. a
# four-part version number that looks like an IP). Reviewers check every use.
SUPPRESS = "hygiene: ok"

PLANTED = "PLANTED-SECRET"
# The key must END in one of these words: client_secret, UNIFI_PASSWORD, and
# ssh_key match; tokenizer, secret_id, token_file and compass_heading don't.
# Any UniFi-style x_ field (x_authkey, x_vwirekey, x_passphrase) counts too.
KEY_WORDS = (
    r"(?:x_\w+|\w*(?:password|passwd|passphrase|secret|token|api[_-]?key|auth[_-]?key|_key))"
)
# Linear matchers for the three credential patterns whose naive form
# backtracks badly. Each naive form starts with an unbounded ``\w*`` (or
# similar star) with no literal anchor, so on a long run of word characters
# every start position re-scans the rest: ``b"g" * 100_000`` took minutes.
# The matchers below find the same matches in linear time:
#
# - ``_credential_assignment_spans``: a match's key ends with a keyword
#   literal (``password`` … ``_key``, ``api-key`` …) or ``x_`` + word
#   characters, and the keyword always ends at the end of a maximal
#   ``[\w-]+`` run: what follows the key is a quote, space, or ``:``/``=``,
#   none of which continue the run. So per run there is one end position;
#   each keyword ending there (case-insensitive literal match, plus the
#   last ``x_`` in the final segment) is a candidate, the winner is the
#   leftmost start (then the longest key — the naive backtracking order),
#   and one ``match()`` for the deterministic remainder decides it.
#   Matches are emitted in order with finditer's consumption (runs inside
#   a match are skipped), which is the same set finditer yields.
# - ``_unquoted_assignment_spans``: the naive form is anchored at line
#   starts, so one parse per line (with/without ``export``, exactly the
#   naive backtracking order) plus one ``match()`` for the remainder is
#   the same set.
# - ``_url_credential_spans``: ``[a-z0-9+.-]`` excludes ``:`` and ``/``,
#   so each ``[a-z]`` start reaches at most one ``://`` (the first in its
#   run) and each ``://`` is reached only from its own run; grouping by
#   ``://`` and taking the leftmost ``[a-z]`` start not yet consumed is
#   the same set. The right side stops at ``/`` (every later ``://``
#   holds one), so per-``://`` work is disjoint.
#
# Each yields ``(group0, value)`` exactly as the naive finditer would
# (same spans, same values; the unquoted group0 starts at the key rather
# than at the line's leading spaces, which can hold no PLANTED marker),
# so PLANTED and reference-value handling in ``secret_problems`` is
# unchanged. A differential fuzz test in tests/test_hygiene.py pins
# equality with the naive forms.
_WORD_CHAR_RE = re.compile(r"\w")
_CRED_REST_RE = re.compile(r"""["']?\s*[:=]\s*(?:"([^"\n]{8,})"|'([^'\n]{8,})')""")
_UNQUOTED_REST_RE = re.compile(r"""["']?[ \t]*[:=][ \t]*([^\s"'#,{}\[\]][^\s#,]{7,})""")
_URL_RIGHT_RE = re.compile(r"""[^/\s:@]+:[^/\s@]+@""")
_URL_LEFT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789+.-")
_URL_SCHEME_FIRST = frozenset("abcdefghijklmnopqrstuvwxyz")
# Keyword literals from KEY_WORDS that may end a key (``x_\w+`` is handled
# separately). Plain literals, so the source line carries no assignment.
_CRED_LITS = (
    "pass" + "word",
    "pass" + "wd",
    "passphrase",
    "sec" + "ret",
    "tok" + "en",
    "api_key",
    "api-key",
    "apikey",
    "auth_key",
    "auth-key",
    "authkey",
    "_key",
)
_CRED_LIT_RES = tuple(re.compile(re.escape(lit), re.IGNORECASE) for lit in _CRED_LITS)
_X_PREFIX_RE = re.compile(r"x_", re.IGNORECASE)
_RUN_RE = re.compile(r"[\w-]+")


def _wordchar_run_start(text: str, pos: int, stop: int) -> int:
    """Left edge of the word-character run ending at ``pos`` (not past ``stop``)."""
    while pos > stop and _WORD_CHAR_RE.match(text, pos - 1) is not None:
        pos -= 1
    return pos


def _key_match_start(text: str, run_start: int, end: int, run_str: str):
    """``(start, key_at)`` of the naive key match ending at ``end``, or None.

    ``run_str`` is the maximal ``[\\w-]+`` run ``text[run_start:end]``; the
    keyword always ends at ``end`` (what follows it is a quote, space, or
    ``:``/``=``). Literals take the naive ``\\w*`` prefix (leftmost start,
    then longest key — the backtracking order); ``x_\\w+`` stands alone in
    the naive alternation, so it starts exactly at its own position.
    Leftmost start wins overall.
    """
    best = None
    for lit_re, lit in zip(_CRED_LIT_RES, _CRED_LITS):
        key_at = end - len(lit)
        if key_at < run_start:
            continue
        lit_match = lit_re.match(text, key_at)
        # The keyword must end exactly at the run end (guards exotic case
        # folds whose length differs from the literal).
        if lit_match is None or lit_match.end() != end:
            continue
        start = _wordchar_run_start(text, key_at, run_start)
        if best is None or start < best[0] or (start == best[0] and key_at > best[1]):
            best = (start, key_at)
    segment = run_str.rsplit("-", 1)[-1]
    seg_base = run_start + len(run_str) - len(segment)
    for prefix in _X_PREFIX_RE.finditer(segment):
        # ``x_`` needs a word character after it inside the run; the
        # ``\\w+`` then runs to the run end on its own. No ``\\w*`` prefix:
        # the naive ``x_\\w+`` branch starts at its own position.
        if prefix.end() < len(segment):
            key_at = seg_base + prefix.start()
            if best is None or key_at < best[0]:
                best = (key_at, key_at)
    return best


def _credential_assignment_spans(text: str) -> list[tuple[str, str | None]]:
    """``(group0, value)`` for key-then-quoted-value assignments."""
    out = []
    consumed = 0
    for run in _RUN_RE.finditer(text):
        run_start, end = run.span()
        if run_start < consumed:
            continue
        best = _key_match_start(text, run_start, end, run.group(0))
        if best is None:
            continue
        rest = _CRED_REST_RE.match(text, end)
        if rest is None:
            continue
        value = rest.group(1) if rest.group(1) is not None else rest.group(2)
        # The naive form's leading ["']? always takes a quote when one is
        # there (a keyword cannot start with a quote), so extend over it.
        start = best[0]
        qstart = start - 1
        prefix = text[qstart] if qstart >= 0 and text[qstart] in "\"'" else ""
        out.append((prefix + text[start : rest.end()], value))
        consumed = rest.end()
    return out


def _unquoted_assignment_spans(text: str) -> list[tuple[str, str | None]]:
    """``(group0, value)`` for key-then-bare-value assignments per line."""
    out = []
    for line in text.split("\n"):
        for with_export in (True, False):
            pos = 0
            size = len(line)
            while pos < size and line[pos] in " \t":
                pos += 1
            if with_export:
                if line[pos : pos + 6].lower() != "export":
                    continue
                pos += 6
                if pos >= size or line[pos] not in " \t":
                    continue
                while pos < size and line[pos] in " \t":
                    pos += 1
            if pos < size and line[pos] in "\"'":
                pos += 1
            if pos >= size or _WORD_CHAR_RE.match(line, pos) is None:
                continue
            run = _RUN_RE.match(line, pos)
            run_start, end = run.span()
            # The naive key starts exactly here: ``\\w*`` cannot reach back
            # over the spaces, and the keyword cannot end mid-run.
            best = _key_match_start(line, run_start, end, run.group(0))
            if best is None or best[0] != pos:
                continue
            rest = _UNQUOTED_REST_RE.match(line, end)
            if rest is None:
                continue
            out.append((line[: rest.end()], rest.group(1)))
            break
    return out


def _url_credential_spans(text: str) -> list[tuple[str, str | None]]:
    """``(group0, None)`` for user:password@ URLs (no capture groups)."""
    out = []
    consumed = 0
    at = text.find("://")
    while at != -1:
        if at >= consumed:
            left = at - 1
            while left >= 0 and text[left] in _URL_LEFT_CHARS:
                left -= 1
            start = max(left + 1, consumed)
            while start < at and text[start] not in _URL_SCHEME_FIRST:
                start += 1
            if start < at:
                right = _URL_RIGHT_RE.match(text, at + 3)
                if right is not None:
                    out.append((text[start : right.end()], None))
                    consumed = right.end()
        at = text.find("://", at + 1)
    return out


SECRET_PATTERNS = [
    ("private key", re.compile("-----BEGIN [A-Z ]*" + "PRIVATE " + "KEY-----")),
    ("Muse SDK token", re.compile("mg" + r"st_[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}")),
    ("credentials in a URL", _url_credential_spans),
    ("JSON web token", re.compile("ey" + r"J[A-Za-z0-9_-]{8,}\.ey" + r"J[A-Za-z0-9_-]{8,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+(?=[\w.~+/-]*\d)[\w.~+/-]{16,}")),
    (
        "session cookie",
        re.compile(r"(?i)\b(?:unifises|token|csrf_token|session(?:id)?)=(?=[^;\s\"']*\d)[^;\s\"']{8,}"),
    ),
    (
        "CSRF token value",
        re.compile(
            r"(?i)x-(?:updated-)?csrf-token[\"']?\s*:\s*[\"']?(?=[\w-]*\d)[\w-]{16,}"
        ),
    ),
    (
        "credential assignment",
        # Key, then : or =, then a quoted value of 8+ characters.
        _credential_assignment_spans,
    ),
]
# YAML, INI, and env files also allow unquoted values. Not applied to Python,
# where an unquoted value is code (password = ctx.secret("unifi_password")).
UNQUOTED_ASSIGNMENT = _unquoted_assignment_spans
UNQUOTED_EXEMPT_SUFFIXES = {".py"}
# Values that name or reference a secret rather than contain one.
REFERENCE_VALUE = re.compile(
    r"^(?:\$\{\{.*\}\}"  # ${{ github.token }}
    r"|\$\{?\w+\}?"  # $VAR or ${VAR}
    r"|<[^>]+>"  # <placeholder>
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"  # snake_case name of a secret file, e.g. unifi_password
    r"|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+"  # a header name, e.g. X-CSRF-Token
    r"|(?:~|\.{1,2})?/\S*"  # a path, e.g. ~/.config/housesitter/secrets/unifi
    r"|(?i:changeme|change-me|example|redacted|placeholder|x{4,}|\*{4,}))$"  # placeholders
)
SSID_KEYS = {"ssid", "essid"}


def decode_bytes(data: bytes) -> str | None:
    """Decode blob or file bytes the way the scan reads them; None if binary."""
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def is_fixture_rel(rel_posix: str) -> bool:
    """Whether a repo-relative POSIX path gets the fixture-only checks."""
    return rel_posix.startswith("tests/fixtures/") and PurePosixPath(rel_posix).suffix == ".json"


def mac_problems(text: str, bare: bool = False) -> list[str]:
    found = []
    for pattern in MAC_RES + ([BARE_MAC_RE] if bare else []):
        for match in pattern.finditer(text):
            digits = re.sub("[^0-9a-f]", "", match.group(0).lower())
            if pattern is BARE_MAC_RE and digits.isdigit():
                continue  # a 12-digit number, e.g. a byte counter
            if not digits.startswith(ALLOWED_MAC_PREFIXES):
                found.append(f"MAC {match.group(0)}")
    return found


def ip_problems(text: str) -> list[str]:
    found = []
    for match in IPV4_RE.finditer(text):
        parts = [int(p) for p in match.group(0).split(".")]
        if any(p > 255 for p in parts):
            continue  # e.g. a version like 6.6.77.15402
        addr = ipaddress.ip_address(".".join(map(str, parts)))  # tolerates leading zeros
        if not any(addr in net for net in ALLOWED_NETS):
            found.append(f"IPv4 {match.group(0)}")
    for match in IPV6_RE.finditer(text):
        candidate = match.group(0)
        if sum(1 for group in candidate.split(":") if group) < 2:
            continue  # e.g. Python slices like [::2]
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not any(addr in net for net in ALLOWED_NETS):
            found.append(f"IPv6 {candidate}")
    return found


def _regex_spans(pattern, text: str):
    """``(group0, value)`` for one compiled pattern, as finditer yields."""
    for match in pattern.finditer(text):
        groups = match.groups()
        value = next((g for g in groups if g), None) if groups else None
        yield match.group(0), value


def secret_problems(text: str, unquoted: bool = True) -> list[str]:
    found = []
    patterns = SECRET_PATTERNS + (
        [("credential assignment", UNQUOTED_ASSIGNMENT)] if unquoted else []
    )
    for label, pattern in patterns:
        # A callable entry is a linear matcher (see above) yielding the
        # same (group0, value) pairs its naive regex would.
        spans = pattern(text) if callable(pattern) else _regex_spans(pattern, text)
        for group0, value in spans:
            if PLANTED in group0:
                continue
            if value is not None and REFERENCE_VALUE.match(value.strip()):
                continue
            found.append(f"likely secret ({label})")
    return found


def ssid_problems(obj: object) -> list[str]:
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in SSID_KEYS and isinstance(value, str) and value:
                if not value.endswith("-example"):
                    found.append(f"SSID {value!r} does not end in -example")
            found.extend(ssid_problems(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(ssid_problems(item))
    return found


def scan_text(rel_posix: str, text: str) -> list[str]:
    """Scan decoded text of the file at repo-relative ``rel_posix``."""
    lines = [line for line in text.splitlines() if SUPPRESS not in line]
    checked = "\n".join(lines)
    suffix = PurePosixPath(rel_posix).suffix
    problems = (
        mac_problems(checked, bare=is_fixture_rel(rel_posix))
        + ip_problems(checked)
        + secret_problems(checked, unquoted=suffix not in UNQUOTED_EXEMPT_SUFFIXES)
    )
    if is_fixture_rel(rel_posix):
        try:
            problems += ssid_problems(json.loads(text))
        except ValueError:
            problems.append("fixture is not valid JSON")
        except RecursionError:
            problems.append("fixture JSON is too deeply nested to check")
    return problems


def scan_bytes(rel_posix: str, data: bytes) -> list[str]:
    """Scan one file's bytes; ``rel_posix`` is its repo-relative POSIX path."""
    text = decode_bytes(data)
    if text is None:
        return []
    return scan_text(rel_posix, text)


def suppression_lines(text: str) -> list[tuple[int, str]]:
    """Every ``(lineno, line)`` carrying the ``hygiene: ok`` marker (1-based)."""
    return [(n, line) for n, line in enumerate(text.splitlines(), 1) if SUPPRESS in line]


# Trusted scan of a git tree (the ci-guard job) --------------------------------
#
# The workflow fetches nothing to disk and checks out nothing: it reads the PR
# head commit's blobs through git plumbing and hands them to the scanner above
# as (path, bytes) pairs. PR content is inert data throughout: the tree SHA
# comes from the environment, is validated as 40 hex chars, and is only ever
# passed as subprocess argv entries (never a shell, never eval, never a file
# name used as code). ``python -I`` additionally ignores environment-driven
# interpreter configuration, so nothing the PR controls can be imported.

# Bounds so a huge PR tree cannot hang or exhaust the job. The whole main tree
# is ~150 files / ~4 MB with no blob over 600 KB; the caps leave wide headroom
# and fail closed (exit 2) instead of skipping silently when exceeded.
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 64_000_000
MAX_TREE_ENTRIES = 100_000
# An overall wall-clock deadline for the tree scan: regexes are linear, but a
# hostile tree must not stall the job. Checked between phases and blobs.
SCAN_DEADLINE_S = 300
# Symlink chains (link -> link -> ...) are followed this far, then refused.
MAX_SYMLINK_HOPS = 40
# Problem and suppression listings are capped so a hostile tree cannot flood
# the workflow log; the counts stay exact.
MAX_LISTED_PROBLEMS = 200
MAX_LISTED_SUPPRESSIONS = 1000


class FailClosed(Exception):
    """The tree could not be established or bounded: refuse, don't pass."""


def _git(args: list[str], *, cwd: str) -> bytes:
    """Run git plumbing with no shell; ``args`` are data, never code."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, check=False, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FailClosed(f"git {' '.join(args[:3])} did not complete: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[:200].strip()
        raise FailClosed(f"git {' '.join(args[:3])} failed ({detail})")
    return proc.stdout


def _display(path: str) -> str:
    """Make a blob path safe for a single log line.

    Surrogates from non-UTF-8 names become ``?`` so printing never raises
    ``UnicodeEncodeError``; valid UTF-8 (including non-ASCII) passes
    through, and newlines cannot inject fake annotations.
    """
    safe = path.encode("utf-8", errors="replace").decode("utf-8")
    return safe.replace("\n", "\\n").replace("\r", "\\r")


def fetch_pr_head(*, cwd: str) -> str:
    """Fetch the PR head commit as objects only and return its SHA.

    Reads ``PR_NUMBER`` and ``HEAD_SHA`` from the environment (the workflow
    passes ``github.event`` values via ``env:``, never interpolated into the
    script). Fails closed unless the fetched commit's SHA exactly matches
    ``HEAD_SHA``: the ``refs/pull/<N>/head`` ref can move between the event
    and the fetch, and scanning anything else would check the wrong tree.
    """
    pr_number = os.environ.get("PR_NUMBER", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    if not re.fullmatch(r"[1-9][0-9]*", pr_number):
        raise FailClosed("PR_NUMBER is missing or not a positive integer")
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise FailClosed("HEAD_SHA is missing or not a 40-char hex SHA")
    _git(["fetch", "--no-tags", "--depth=1", "origin", f"refs/pull/{pr_number}/head"], cwd=cwd)
    fetched = _git(["rev-parse", "--verify", "FETCH_HEAD"], cwd=cwd).decode().strip()
    if fetched != head_sha:
        raise FailClosed("fetched head does not match HEAD_SHA; the PR moved, re-run the job")
    kind = _git(["cat-file", "-t", head_sha], cwd=cwd).decode().strip()
    if kind != "commit":
        raise FailClosed(f"HEAD_SHA is a {kind or 'missing object'}, not a commit")
    return head_sha


def _check_deadline(start: float, what: str) -> None:
    """Fail closed when the scan's wall-clock deadline has passed."""
    if time.monotonic() - start > SCAN_DEADLINE_S:
        raise FailClosed(
            f"hygiene scan exceeded its {SCAN_DEADLINE_S}s deadline while {what}; "
            "refusing to scan"
        )


# Size of one read() from the ls-tree pipe. Tests shrink it to force
# splits inside records.
_LS_TREE_CHUNK = 65536

# A record's metadata (before the TAB): mode, object type, SHA, and the
# space-padded size ("-" for gitlinks). The type is ``blob`` or ``commit``
# (``-r`` never lists trees); anything else is a corrupt listing.
_TREE_RECORD_RE = re.compile(r"([0-7]{6}) (blob|commit) ([0-9a-f]{40}) +([0-9]+|-)")

# Modes git can list here: plain files, executables, symlinks, gitlinks.
_FILE_MODES = ("100644", "100755")


def _parse_tree_record(record: bytes) -> tuple[str, str, str, int | None, str]:
    """Split one NUL-terminated ls-tree record; fail closed on anything else.

    Returns ``(mode, objtype, sha, size, path)`` with ``size`` None for a
    gitlink. Records split only on NUL, so a forged ``100644 blob
    <clean> ...`` inside a filename stays path bytes; the metadata before
    the first TAB must then match strictly.
    """
    meta, sep, raw_path = record.partition(b"\t")
    if not sep or not raw_path:
        raise FailClosed("could not parse the tree listing; refusing to scan")
    try:
        meta_text = meta.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FailClosed(
            "could not parse the tree listing; refusing to scan"
        ) from exc
    match = _TREE_RECORD_RE.fullmatch(meta_text)
    if match is None:
        raise FailClosed("could not parse the tree listing; refusing to scan")
    mode, objtype, sha, size = match.groups()
    if mode == "120000":
        if objtype != "blob":
            raise FailClosed("could not parse the tree listing; refusing to scan")
    elif mode == "160000":
        if objtype != "commit":
            raise FailClosed("could not parse the tree listing; refusing to scan")
    elif mode in _FILE_MODES:
        if objtype != "blob":
            raise FailClosed("could not parse the tree listing; refusing to scan")
    else:
        raise FailClosed("could not parse the tree listing; refusing to scan")
    if objtype == "commit":
        if size != "-":
            raise FailClosed("could not parse the tree listing; refusing to scan")
        size_int: int | None = None
    else:
        try:
            size_int = int(size)
        except ValueError as exc:
            raise FailClosed(
                "could not parse the tree listing; refusing to scan"
            ) from exc
    # Decoded like the filesystem; only the ASCII prefix and suffix the
    # checks rely on survive any byte sequence.
    return mode, objtype, sha, size_int, os.fsdecode(raw_path)


def _iter_tree_records(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Yield complete NUL-terminated records from raw ls-tree output.

    A record split across two reads is kept whole: only bytes up to a NUL
    are yielded, and the incomplete tail waits for the next chunk. Any
    trailing bytes without a terminator are a corrupt listing.
    """
    buf = b""
    for chunk in chunks:
        buf += chunk
        while True:
            cut = buf.find(b"\0")
            if cut == -1:
                break
            yield buf[:cut]
            buf = buf[cut + 1 :]
    if buf:
        raise FailClosed("could not parse the tree listing; refusing to scan")


def list_tree_blobs(
    sha: str, *, cwd: str
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]], list[str]]:
    """List entries in the tree at ``sha``.

    Returns ``(files, links, submodules)``: ``(blob_sha, size, path)`` for
    regular blobs, the same for mode-120000 symlink blobs, and the paths of
    skipped submodule (gitlink) entries. Submodules are directories, not
    files: the working-tree scan never sees inside them either, so they
    are skipped, not scanned.

    Paths come from plain ``git ls-tree -r -z --long`` output, whose
    NUL-terminated records carry raw path bytes: unlike ``--format=%(path)``,
    nothing here is C-quoted, so a name holding a tab, newline, quote, or
    backslash still matches the fixture prefix and ``.json`` suffix checks.
    Names are decoded like the filesystem (``os.fsdecode``): the ASCII
    prefix and suffix the checks rely on survive any byte sequence, and no
    hostile name can confuse them.

    The listing is streamed and the entry cap is enforced while reading, so
    a compact tree expanding to millions of paths fails closed instead of
    exhausting memory. The object type is checked before the size: gitlink
    entries carry ``-`` as their size. Fails closed when the entry count,
    the total bytes, or any single blob exceeds the bounds above.
    """
    proc = subprocess.Popen(
        ["git", "ls-tree", "-r", "-z", "--long", "--full-tree", sha],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    listed = time.monotonic()
    files: list[tuple[str, int, str]] = []
    links: list[tuple[str, int, str]] = []
    submodules: list[str] = []
    total = 0
    entries = 0

    def read_chunks() -> Iterator[bytes]:
        while True:
            if time.monotonic() - listed > 300:
                raise FailClosed("git ls-tree did not complete; refusing to scan")
            try:
                chunk = proc.stdout.read(_LS_TREE_CHUNK)
            except OSError as exc:
                raise FailClosed(
                    f"could not read the tree listing ({exc}); refusing to scan"
                ) from exc
            if not chunk:
                return
            yield chunk

    try:
        for record in _iter_tree_records(read_chunks()):
            if time.monotonic() - listed > 300:
                raise FailClosed("git ls-tree did not complete; refusing to scan")
            if not record:
                raise FailClosed(
                    "could not parse the tree listing; refusing to scan"
                )
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise FailClosed(
                    f"tree lists more than {MAX_TREE_ENTRIES} entries "
                    "(limit reached while reading); refusing to scan"
                )
            mode, objtype, objsha, size_or_none, path = _parse_tree_record(record)
            # A gitlink is a directory, not a file: skipped, like the
            # working-tree scan, which never sees inside submodules either.
            if objtype == "commit":
                submodules.append(path)
                continue
            assert size_or_none is not None  # blobs always carry a size
            size_int = size_or_none
            if size_int > MAX_FILE_BYTES:
                raise FailClosed(
                    f"blob {_display(path)} is {size_int} bytes "
                    f"(limit {MAX_FILE_BYTES}); refusing to scan"
                )
            total += size_int
            if total > MAX_TOTAL_BYTES:
                raise FailClosed(
                    f"tree holds more than {MAX_TOTAL_BYTES} bytes; refusing to scan"
                )
            if mode == "120000":
                links.append((objsha, size_int, path))
            else:
                files.append((objsha, size_int, path))
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise FailClosed("git ls-tree did not complete; refusing to scan") from exc
        if proc.returncode != 0:
            detail = (proc.stderr.read(200) if proc.stderr else b"").decode(
                "utf-8", errors="replace"
            ).strip()
            raise FailClosed(f"git ls-tree failed ({detail})")
    finally:
        try:
            proc.stdout.close()
        except (AttributeError, ValueError):
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return files, links, submodules


def _normalize_link_target(link_path: str, target: str) -> str:
    """Resolve a relative symlink target against the link's directory.

    Absolute targets and escapes past the tree root fail closed: they cannot
    be resolved inside the tree. The target text is never quoted in
    messages: a link blob is PR-controlled data and could carry a secret.
    """
    if target.startswith("/"):
        raise FailClosed(
            f"symlink {_display(link_path)} has an absolute target outside "
            "the tree; refusing to scan"
        )
    stack = link_path.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise FailClosed(
                    f"symlink {_display(link_path)} escapes the tree root; "
                    "refusing to scan"
                )
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack)


def _resolve_symlink(
    link_path: str,
    target: str,
    kinds: dict,
    link_targets: dict,
    dir_paths: set,
    submodule_paths: list,
) -> tuple | None:
    """Follow one symlink to a tree file, like the working-tree scan does.

    Returns ``(blob_sha, size)`` for the target file, ``None`` when the
    target is a directory (or under a submodule: contents the working-tree
    scan never sees either), and raises :class:`FailClosed` for absolute,
    escaping, dangling, cyclic, or over-long chains.
    """
    seen = {link_path}
    current = _normalize_link_target(link_path, target)
    for _ in range(MAX_SYMLINK_HOPS):
        if current in dir_paths or any(
            current == sub or current.startswith(sub + "/") for sub in submodule_paths
        ):
            return None
        entry = kinds.get(current)
        if entry is None:
            raise FailClosed(
                f"symlink {_display(link_path)} points outside the tree "
                "(dangling); refusing to scan"
            )
        kind, sha, size = entry
        if kind == "submodule":
            return None
        if kind == "file":
            return (sha, size)
        if current in seen:
            raise FailClosed(
                f"symlink {_display(link_path)} loops back on itself "
                "(cycle); refusing to scan"
            )
        seen.add(current)
        current = _normalize_link_target(current, link_targets[current])
    raise FailClosed(
        f"symlink {_display(link_path)} exceeds {MAX_SYMLINK_HOPS} hops; refusing to scan"
    )


def read_blobs(blobs: list[tuple[str, int, str]], *, cwd: str) -> list[bytes]:
    """Read every blob's bytes in one ``git cat-file --batch`` session."""
    if not blobs:
        return []
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=cwd,
        input="".join(f"{sha}\n" for sha, _, _ in blobs).encode("ascii"),
        capture_output=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace")[:200].strip()
        raise FailClosed(f"git cat-file --batch failed ({detail})")
    contents = []
    rest = proc.stdout
    for sha, size, path in blobs:
        head, _, rest = rest.partition(b"\n")
        try:
            found, objtype, found_size = head.decode("ascii").split(" ")
            found_size = int(found_size)
        except ValueError as exc:
            raise FailClosed(
                "could not parse git cat-file --batch output; refusing to scan"
            ) from exc
        if found != sha or objtype != "blob" or found_size != size:
            raise FailClosed(f"blob {_display(path)} changed mid-read; refusing to scan")
        contents.append(rest[:size])
        rest = rest[size:]
        if not rest.startswith(b"\n"):
            raise FailClosed("could not parse git cat-file --batch output; refusing to scan")
        rest = rest[1:]
    return contents


def scan_git_tree(
    sha: str, *, cwd: str
) -> tuple[list[str], list[str], int, int, int]:
    """Scan every file in the tree at ``sha``. Returns ``(problems,
    suppressions, files_scanned, bytes_scanned, submodules_skipped)``;
    problems are display lines of the form ``path: problem`` and
    suppressions of the form ``path:lineno`` (never the line content: it
    may hold the suppressed secret).

    Symlinks are resolved within the tree and the target blob's content is
    scanned under the link's own name, exactly as the working-tree scan
    follows file symlinks; links to directories are skipped, and links
    that cannot resolve inside the tree fail closed.
    """
    start = time.monotonic()
    _check_deadline(start, "starting")
    files, links, submodules = list_tree_blobs(sha, cwd=cwd)
    _check_deadline(start, "listing the tree")
    contents = read_blobs(files, cwd=cwd)
    link_contents = read_blobs(links, cwd=cwd)
    _check_deadline(start, "reading the blobs")
    kinds: dict = {}
    for objsha, size, path in files:
        kinds[path] = ("file", objsha, size)
    for objsha, size, path in links:
        kinds[path] = ("link", objsha, size)
    submodule_paths = submodules
    for path in submodule_paths:
        kinds[path] = ("submodule", "", 0)
    dir_paths = set()
    for _, _, path in files + links:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dir_paths.add("/".join(parts[:depth]))
    bytes_by_sha = {}
    for (objsha, _, _), data in zip(files, contents):
        bytes_by_sha.setdefault(objsha, data)
    link_targets = {}
    for (_, _, path), data in zip(links, link_contents):
        link_targets[path] = os.fsdecode(data)
    problems = []
    suppressions = []
    scanned_bytes = 0

    def check(path: str, data: bytes, size: int) -> None:
        _check_deadline(start, "scanning the blobs")
        nonlocal scanned_bytes
        scanned_bytes += size
        text = decode_bytes(data)
        if text is None:
            return
        for problem in scan_text(path, text):
            problems.append(f"{_display(path)}: {problem}")
        for lineno, _ in suppression_lines(text):
            suppressions.append(f"{_display(path)}:{lineno}")

    for (_, size, path), data in zip(files, contents):
        check(path, data, size)
    for (_, _, link_path), _ in zip(links, link_contents):
        target = _resolve_symlink(
            link_path, link_targets[link_path], kinds, link_targets, dir_paths, submodule_paths
        )
        if target is None:
            continue
        sha_found, size_found = target
        check(link_path, bytes_by_sha[sha_found], size_found)
    return problems, suppressions, len(files) + len(links), scanned_bytes, len(submodule_paths)


def escape_data(text: str) -> str:
    """Escape a value for a GitHub workflow command (``::error::``)."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Exit 0: clean; 1: problems found; 2: fail closed."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--git-tree", metavar="SHA", help="scan the blobs of one local tree")
    group.add_argument(
        "--fetch-and-scan-pr-head",
        action="store_true",
        help="fetch refs/pull/$PR_NUMBER/head, verify it equals $HEAD_SHA, scan it",
    )
    args = parser.parse_args(argv)
    cwd = os.getcwd()
    try:
        sha = fetch_pr_head(cwd=cwd) if args.fetch_and_scan_pr_head else args.git_tree
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise FailClosed("--git-tree needs a 40-char hex SHA")
        problems, suppressions, files_scanned, bytes_scanned, submodules = scan_git_tree(
            sha, cwd=cwd
        )
    except FailClosed as exc:
        print(f"::error::{escape_data(f'hygiene-scan: fail closed: {exc}')}")
        return 2
    print(f"hygiene-scan: scanned {files_scanned} file(s), {bytes_scanned} byte(s)")
    if submodules:
        print(
            "hygiene-scan: skipped "
            f"{submodules} submodule(s) (directories, not scanned)"
        )
    shown = problems[:MAX_LISTED_PROBLEMS]
    for problem in shown:
        print(f"::error::{escape_data(f'hygiene-scan: {problem}')}")
    if len(problems) > len(shown):
        more = len(problems) - len(shown)
        print(f"::error::{escape_data(f'hygiene-scan: ... and {more} more problem(s)')}")
    print(f"hygiene-scan: {len(suppressions)} line(s) carry {SUPPRESS!r}:")
    for line in suppressions[:MAX_LISTED_SUPPRESSIONS]:
        print(f"hygiene-scan: suppression {line}")
    if len(suppressions) > MAX_LISTED_SUPPRESSIONS:
        more = len(suppressions) - MAX_LISTED_SUPPRESSIONS
        print(f"hygiene-scan: ... and {more} more suppression(s)")
    if not suppressions:
        print("hygiene-scan: no suppression lines")
    if problems:
        print(f"::error::{escape_data(f'hygiene-scan: {len(problems)} problem(s) found')}")
        return 1
    print("hygiene-scan: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

