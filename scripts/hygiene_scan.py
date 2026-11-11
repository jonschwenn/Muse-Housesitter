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
import bisect
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
# Linear matchers for the five patterns whose naive form backtracks
# badly. Each naive form re-scans the rest of the input from every start
# position on a failure, so a long hostile run is quadratic: ``b"g" *
# 100_000`` took minutes for the credential forms, ``b"eyJ" * n`` takes
# 0.25/1.0/4.0 s at 30/60/120 KB, and ``"token=-" * 20_000`` exceeds 10 s.
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
# - ``_jwt_token_spans``: the greedy first half consumes exactly the
#   ``[A-Za-z0-9_-]`` run holding ``start + 3`` (backtracking cannot cross
#   the run's first non-class character), so each start has one candidate
#   dot — the run's end — and the trailing half is one run lookup.
# - ``_session_cookie_spans``: the key is the maximal ``\w`` run ending at
#   ``=`` (only a whole keyword matches, as in the naive form) and the
#   value is the one ``[^;\s\"']`` run after it, so the lookahead is one
#   search inside one run and per-``=`` work is disjoint.
#
# Each yields ``(group0, value, start)`` exactly as the naive finditer would
# (same spans, same values, same starts; the unquoted group0 starts at the
# key rather than at the line's leading spaces, which can hold no PLANTED
# marker), so PLANTED and reference-value handling in ``secret_problems``
# is unchanged. ``secret_problems`` reports the line of ``start`` instead
# of the value, so secrets never reach the log. The token and cookie
# matchers expose span lists (``_jwt_token_spans``,
# ``_session_cookie_spans``) that a differential fuzz test in
# tests/test_hygiene.py pins to the naive forms span for span.
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


def _key_match_possible(run_str: str) -> bool:
    """Whether a keyword could end at this run's end (exact pre-filter).

    Every keyword tail is ASCII (``password`` ends ``d``, ``token`` ends
    ``n``, ``x_\\w+`` needs a literal ``x_``), and regex IGNORECASE on an
    ASCII run matches exactly the ASCII case pairs — so an ASCII run whose
    lowered end is no tail and holds no ``x_`` can match nothing below.
    Non-ASCII runs take the slow path. Callers still run the full decider,
    so this only skips what it proves impossible.
    """
    if not run_str.isascii():
        return True
    lowered = run_str.lower()
    return lowered.endswith(_CRED_LITS) or "x_" in lowered


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


def _credential_assignment_spans(text: str) -> list[tuple[str, str | None, int]]:
    """``(group0, value, start)`` for key-then-quoted-value assignments."""
    out = []
    consumed = 0
    for run in _RUN_RE.finditer(text):
        run_start, end = run.span()
        if run_start < consumed:
            continue
        # The remainder must open with a quote, space, or ``:``/``=``; any
        # other follower (``.``, ``-``, a letter) fails the rest match, so
        # the twelve keyword trials below are skipped. Exact: the naive
        # ``["']?\\s*[:=]`` prefix cannot match anything else.
        if end < len(text):
            follower = text[end]
            if follower not in "\"':=" and not follower.isspace():
                continue
        run_str = run.group(0)
        if not _key_match_possible(run_str):
            continue
        best = _key_match_start(text, run_start, end, run_str)
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
        if qstart >= 0 and text[qstart] in "\"'":
            group_start, prefix = qstart, text[qstart]
        else:
            group_start, prefix = start, ""
        out.append((prefix + text[start : rest.end()], value, group_start))
        consumed = rest.end()
    return out


def _unquoted_assignment_spans(text: str) -> list[tuple[str, str | None, int]]:
    """``(group0, value, start)`` for key-then-bare-value assignments per line."""
    out = []
    offset = 0
    for line in text.split("\n"):
        # The naive form needs a ``:`` or ``=`` after the key; a line with
        # neither cannot match. Exact, and lines of bare hits skip the rest.
        if "=" not in line and ":" not in line:
            offset += len(line) + 1
            continue
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
            run_str = run.group(0)
            if not _key_match_possible(run_str):
                continue
            best = _key_match_start(line, run_start, end, run_str)
            if best is None or best[0] != pos:
                continue
            rest = _UNQUOTED_REST_RE.match(line, end)
            if rest is None:
                continue
            out.append((line[: rest.end()], rest.group(1), offset))
            break
        offset += len(line) + 1
    return out


def _url_credential_spans(text: str) -> list[tuple[str, str | None, int]]:
    """``(group0, None, start)`` for user:password@ URLs (no capture groups)."""
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
                    out.append((text[start : right.end()], None, start))
                    consumed = right.end()
        at = text.find("://", at + 1)
    return out


_JWT_HEAD = "ey" + "J"
_JWT_RUN_RE = re.compile(r"[A-Za-z0-9_-]+")


def _jwt_token_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` for JSON web tokens, linear in the input length.

    The same spans the naive ``eyJ…\\.eyJ…`` regex finds. Per start the
    naive form re-scans the rest on every failure, so ``b"eyJ" * n`` is
    quadratic. Instead each maximal ``[A-Za-z0-9_-]`` run is visited once:
    the regex's greedy first half consumes exactly the run holding
    ``start + 3`` (backtracking cannot cross the run's first non-class
    character), so there is one candidate dot per start — the run's end —
    and the trailing half is one run lookup. Starts inside a consumed match
    are skipped, like finditer's non-overlap.
    """
    # Every span holds the head literal; without it there is nothing to find.
    # Exact: the naive regex cannot match either.
    if _JWT_HEAD not in text:
        return []
    runs = [(m.start(), m.end()) for m in _JWT_RUN_RE.finditer(text)]
    out = []
    consumed = 0
    ri = 0
    nruns = len(runs)
    start = text.find(_JWT_HEAD)
    while start != -1:
        if start < consumed:
            start = text.find(_JWT_HEAD, start + 1)
            continue
        while ri < nruns and runs[ri][1] <= start + 3:
            ri += 1
        if ri < nruns and runs[ri][0] <= start + 3 < runs[ri][1]:
            run_end = runs[ri][1]
            if (
                run_end - (start + 3) >= 8
                and text.startswith(".", run_end)
                and text.startswith(_JWT_HEAD, run_end + 1)
            ):
                rj = ri
                while rj < nruns and runs[rj][1] <= run_end + 4:
                    rj += 1
                if (
                    rj < nruns
                    and runs[rj][0] <= run_end + 4 < runs[rj][1]
                    and runs[rj][1] - (run_end + 4) >= 8
                ):
                    trail_end = runs[rj][1]
                    out.append((start, trail_end))
                    consumed = trail_end
                    start = text.find(_JWT_HEAD, trail_end)
                    continue
        start = text.find(_JWT_HEAD, start + 1)
    return out


_COOKIE_SEG_RE = re.compile(r"[^;\s\"']+")
_COOKIE_DIGIT_RE = re.compile(r"\d")
_COOKIE_ALT_RE = re.compile(r"(?:unifises|token|csrf_token|sessionid|session)", re.IGNORECASE)


def _session_cookie_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` for session cookies, linear in the input length.

    The same spans the naive ``\\b(?:unifises|token|…)`` regex finds. Per
    ``=`` the naive lookahead re-scans the rest on every failure, so
    ``"token=-" * n`` is quadratic. Instead each maximal
    ``[^;\\s\\"']`` run is visited once: the key is the maximal ``\\w``
    run ending at ``=`` (the ``\\b`` is automatic, and only a whole
    keyword matches — a longer run like ``mytoken`` can never match, as in
    the naive form), the value is the one run starting at ``= + 1``, and
    the digit lookahead is one precomputed flag per run. ``=`` inside a
    consumed match is skipped, like finditer's non-overlap. (One search per
    ``=`` would re-scan a shared run every time — the naive form's own
    quadratic trap.)
    """
    # Every span holds ``=``; without it there is nothing to find. Exact:
    # the naive regex cannot match either.
    if "=" not in text:
        return []
    runs = [(m.start(), m.end()) for m in _COOKIE_SEG_RE.finditer(text)]
    # The naive ``[^;\s\"']*\d`` lookahead succeeds exactly when a digit
    # follows ``=`` inside the value run: digit positions per run, up front
    # (a digit before the value, e.g. ``1-token=abcdefgh``, must not
    # count), then one bisection per ``=``.
    run_digits = [
        [m.start() for m in _COOKIE_DIGIT_RE.finditer(text, start, end)]
        for start, end in runs
    ]
    out = []
    consumed = 0
    ri = 0
    nruns = len(runs)
    pos = text.find("=")
    while pos != -1:
        if pos < consumed:
            pos = text.find("=", pos + 1)
            continue
        word_start = _wordchar_run_start(text, pos, 0)
        if _COOKIE_ALT_RE.fullmatch(text[word_start:pos]) is None:
            pos = text.find("=", pos + 1)
            continue
        while ri < nruns and runs[ri][1] <= pos + 1:
            ri += 1
        if ri < nruns and runs[ri][0] <= pos + 1 < runs[ri][1]:
            digits = run_digits[ri]
            at = bisect.bisect_left(digits, pos + 1)
            seg_ok = runs[ri][1] - (pos + 1) >= 8 and at < len(digits)
        else:
            seg_ok = False
        if seg_ok:
            seg_end = runs[ri][1]
            out.append((word_start, seg_end))
            consumed = seg_end
            pos = text.find("=", seg_end)
            continue
        pos = text.find("=", pos + 1)
    return out


def _jwt_token_matches(text: str) -> list[tuple[str, str | None, int]]:
    """``(group0, None, start)`` for JSON web tokens (no capture groups)."""
    return [(text[start:end], None, start) for start, end in _jwt_token_spans(text)]


def _session_cookie_matches(text: str) -> list[tuple[str, str | None, int]]:
    """``(group0, None, start)`` for session cookies (no capture groups)."""
    return [
        (text[start:end], None, start) for start, end in _session_cookie_spans(text)
    ]


SECRET_PATTERNS = [
    ("private key", re.compile("-----BEGIN [A-Z ]*" + "PRIVATE " + "KEY-----")),
    ("Muse SDK token", re.compile("mg" + r"st_[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}")),
    ("credentials in a URL", _url_credential_spans),
    ("JSON web token", _jwt_token_matches),
    ("bearer token", re.compile(r"(?i)\bbearer\s+(?=[\w.~+/-]*\d)[\w.~+/-]{16,}")),
    ("session cookie", _session_cookie_matches),
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


def _line_lookup(text: str):
    """``pos -> 1-based line number`` for ``text``, built in one linear pass.

    Counting newlines from the start per match (``text.count`` per problem)
    is quadratic over a file with a hit on every line; the line-start
    offsets are built once per file here and each lookup bisects them.
    Problem messages point, never quote.
    """
    starts = [0]
    append = starts.append
    pos = text.find("\n")
    while pos != -1:
        append(pos + 1)
        pos = text.find("\n", pos + 1)

    def line_of(match_pos: int) -> int:
        return bisect.bisect_right(starts, match_pos)

    return line_of


def _report_line(line_of, pos: int, line_map: list[int] | None) -> int:
    """File line for a match in suppression-filtered ``text``.

    Detection runs on the filtered text, but the reported line must point
    at the file: ``line_map`` takes each filtered line to its file line.
    Without a map the text is the file.
    """
    line = line_of(pos)
    if line_map is not None:
        line = line_map[line - 1]
    return line


# One hostile file can hold a hit on every line; the CI listing is already
# capped, but the per-file strings are built before that. Each check below
# collects at most this many problem strings per file and counts the rest
# exactly, so time and memory stay bounded while the total stays honest.
MAX_PROBLEMS_PER_CHECK = 1000


def is_fixture_rel(rel_posix: str) -> bool:
    """Whether a repo-relative POSIX path gets the fixture-only checks."""
    return rel_posix.startswith("tests/fixtures/") and PurePosixPath(rel_posix).suffix == ".json"


def mac_problems(
    text: str, bare: bool = False, line_map: list[int] | None = None
) -> list[str]:
    # The message names the rule and the line, never the address: values
    # stay out of logs (hard rule 6).
    line_of = _line_lookup(text)
    found = []
    overflow = 0
    for pattern in MAC_RES + ([BARE_MAC_RE] if bare else []):
        for match in pattern.finditer(text):
            digits = re.sub("[^0-9a-f]", "", match.group(0).lower())
            if pattern is BARE_MAC_RE and digits.isdigit():
                continue  # a 12-digit number, e.g. a byte counter
            if not digits.startswith(ALLOWED_MAC_PREFIXES):
                if len(found) < MAX_PROBLEMS_PER_CHECK:
                    found.append(
                        f"MAC address at line {_report_line(line_of, match.start(), line_map)}"
                    )
                else:
                    overflow += 1
    if overflow:
        found.append(f"... and {overflow} more MAC address problem(s) in this file")
    return found


def _ipv4_allowed(parts: list[int]) -> bool:
    """Whether a dotted quad (each part already known to be <= 255) is allowed.

    Integer comparisons against exactly the networks in ALLOWED_NETS, so a
    file with a hit on every line never pays for an ``ipaddress`` parse per
    hit. Leading zeros are already folded by the caller's ``int()``.
    """
    a, b, c, d = parts
    return (
        (a == 192 and b == 0 and c == 2)  # 192.0.2.0/24
        or (a == 198 and b == 51 and c == 100)  # 198.51.100.0/24
        or (a == 203 and b == 0 and c == 113)  # 203.0.113.0/24
        or a == 127  # 127.0.0.0/8
        or (a == 0 and b == 0 and c == 0 and d == 0)  # 0.0.0.0/32
        or a == 255  # 255.0.0.0/8
    )


def _ipv6_allowed_head(candidate: str) -> bool | None:
    """Fast deny for an IPv6 candidate from its first hextet.

    Returns False when the candidate is provably a valid but disallowed
    address, None when a full parse is needed. Validity comes first: the
    slow path skips unparseable candidates (``1:2:3:4:5``, ``fe80::1::2``,
    a trailing single colon like ``fe80::02:00:00:``), so the fast path
    must too — only an exactly-once ``::`` filling at least one group and
    not ending in a single colon, or eight full groups, parses. Then
    ``2001`` may open 2001:db8::/32, ``ff00``-``ffff`` may open ff00::/8,
    and a leading or all-zero group may still be ::1, so those also take
    the slow path. Anything else is valid (regex-shaped groups of at most
    four hex digits) and outside every allowed net.
    """
    if "::" in candidate:
        if candidate.count("::") != 1:
            return None
        if sum(1 for group in candidate.split(":") if group) > 7:
            return None
        if candidate.endswith(":") and not candidate.endswith("::"):
            # A trailing single colon (``fe80::02:00:00:``) is not an
            # address; only a trailing ``::`` compresses. The slow path
            # rejects it, so the fast path must not decide it.
            return None
    else:
        groups = candidate.split(":")
        if len(groups) != 8 or any(group == "" for group in groups):
            return None
    head = candidate.split(":", 1)[0]
    if head == "" or head.strip("0") == "":
        return None
    value = int(head, 16)
    if value == 0x2001 or 0xFF00 <= value <= 0xFFFF:
        return None
    return False


def ip_problems(text: str, line_map: list[int] | None = None) -> list[str]:
    # The message names the rule and the line, never the address.
    line_of = _line_lookup(text)
    found = []
    overflow = 0

    def emit(kind: str, pos: int) -> None:
        nonlocal overflow
        if len(found) < MAX_PROBLEMS_PER_CHECK:
            found.append(f"{kind} at line {_report_line(line_of, pos, line_map)}")
        else:
            overflow += 1

    for match in IPV4_RE.finditer(text):
        parts = [int(p) for p in match.group(0).split(".")]
        if any(p > 255 for p in parts):
            continue  # e.g. a version like 6.6.77.15402
        if not _ipv4_allowed(parts):
            emit("IPv4 address", match.start())
    for match in IPV6_RE.finditer(text):
        candidate = match.group(0)
        if sum(1 for group in candidate.split(":") if group) < 2:
            continue  # e.g. Python slices like [::2]
        decided = _ipv6_allowed_head(candidate)
        if decided is False:
            emit("IPv6 address", match.start())
            continue
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not any(addr in net for net in ALLOWED_NETS):
            emit("IPv6 address", match.start())
    if overflow:
        found.append(f"... and {overflow} more IP address problem(s) in this file")
    return found


def _regex_spans(pattern, text: str):
    """``(group0, value, start)`` for one compiled pattern, as finditer yields."""
    for match in pattern.finditer(text):
        groups = match.groups()
        value = next((g for g in groups if g), None) if groups else None
        yield match.group(0), value, match.start()


def secret_problems(
    text: str, unquoted: bool = True, line_map: list[int] | None = None
) -> list[str]:
    # The message names the rule and the line, never the value: values
    # stay out of logs (hard rule 6).
    line_of = _line_lookup(text)
    found = []
    overflow = 0
    patterns = SECRET_PATTERNS + (
        [("credential assignment", UNQUOTED_ASSIGNMENT)] if unquoted else []
    )
    for label, pattern in patterns:
        # A callable entry is a linear matcher (see above) yielding the
        # same (group0, value, start) triples its naive regex would.
        spans = pattern(text) if callable(pattern) else _regex_spans(pattern, text)
        for group0, value, start in spans:
            if PLANTED in group0:
                continue
            if value is not None and REFERENCE_VALUE.match(value.strip()):
                continue
            if len(found) < MAX_PROBLEMS_PER_CHECK:
                found.append(
                    f"likely secret ({label}) at line {_report_line(line_of, start, line_map)}"
                )
            else:
                overflow += 1
    if overflow:
        found.append(f"... and {overflow} more likely-secret problem(s) in this file")
    return found


def ssid_problems(obj: object) -> list[tuple[str, str]]:
    """``(key, value)`` for SSIDs that do not end in ``-example``.

    Callers report the key's line and the value's length, never the value:
    names are untrusted data and stay out of logs (hard rule 6).
    """
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in SSID_KEYS and isinstance(value, str) and value:
                if not value.endswith("-example"):
                    found.append((key, value))
            found.extend(ssid_problems(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(ssid_problems(item))
    return found


def _ssid_key_line(lines: list[str], key: str) -> int | None:
    """1-based line holding the JSON ``"key"``, if it is on one line."""
    quoted = f'"{key}"'
    for number, line in enumerate(lines, 1):
        if quoted in line:
            return number
    return None


def scan_text(rel_posix: str, text: str) -> list[str]:
    """Scan decoded text of the file at repo-relative ``rel_posix``.

    Problem messages name the rule and the line, never the offending
    value (at most a short shape like ``SSID value (23 chars)``).
    """
    numbered = text.splitlines()
    kept = [number for number, line in enumerate(numbered, 1) if SUPPRESS not in line]
    checked = "\n".join(line for line in numbered if SUPPRESS not in line)
    suffix = PurePosixPath(rel_posix).suffix
    problems = (
        mac_problems(checked, bare=is_fixture_rel(rel_posix), line_map=kept)
        + ip_problems(checked, line_map=kept)
        + secret_problems(
            checked, unquoted=suffix not in UNQUOTED_EXEMPT_SUFFIXES, line_map=kept
        )
    )
    if is_fixture_rel(rel_posix):
        try:
            offenders = ssid_problems(json.loads(text))
        except ValueError:
            problems.append("fixture is not valid JSON")
        except RecursionError:
            problems.append("fixture JSON is too deeply nested to check")
        else:
            # Line numbers point at the file's own lines, not the
            # suppression-filtered text the other checks run on. Offenders
            # usually share one key ("ssid"), so each key's line is found
            # once, not once per offender.
            key_lines: dict[str, int | None] = {}
            ssid_kept = 0
            ssid_overflow = 0
            for key, value in offenders:
                shape = f"SSID value ({len(value)} chars)"
                if key not in key_lines:
                    key_lines[key] = _ssid_key_line(numbered, key)
                at = key_lines[key]
                if at is None:
                    problem = f"{shape} does not end in -example"
                else:
                    problem = f"{shape} at line {at} does not end in -example"
                if ssid_kept < MAX_PROBLEMS_PER_CHECK:
                    problems.append(problem)
                    ssid_kept += 1
                else:
                    ssid_overflow += 1
            if ssid_overflow:
                problems.append(
                    f"... and {ssid_overflow} more SSID problem(s) in this file"
                )
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
MAX_TOTAL_BYTES = 32_000_000
MAX_TREE_ENTRIES = 10_000
# An overall wall-clock deadline for the tree scan: regexes are linear, but a
# hostile tree must not stall the job. Checked between phases and blobs.
SCAN_DEADLINE_S = 300
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


def _redact_path(path: str) -> str:
    """Replace secret-shaped spans in a printed path with ``[redacted]``.

    File names are PR-controlled data, and a token-shaped name would
    otherwise reach the CI log verbatim through the suppression listing or
    a fail-closed error (round 3, finding 4). Every span the secret
    detectors match — token formats, credential-shaped segments — becomes
    the fixed placeholder, before escaping and truncation. The planted
    marker and reference-like values stay readable, exactly as in
    :func:`secret_problems`: they name or excuse a secret rather than
    carry one.
    """
    detectors = SECRET_PATTERNS + [("credential assignment", UNQUOTED_ASSIGNMENT)]
    spans = []
    for _label, pattern in detectors:
        entries = pattern(path) if callable(pattern) else _regex_spans(pattern, path)
        for group0, value, start in entries:
            if PLANTED in group0:
                continue
            if value is not None and REFERENCE_VALUE.match(value.strip()):
                continue
            spans.append((start, start + len(group0)))
    if not spans:
        return path
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts = []
    pos = 0
    for start, end in merged:
        parts.append(path[pos:start])
        parts.append("[redacted]")
        pos = end
    parts.append(path[pos:])
    return "".join(parts)


def _display(path: str) -> str:
    """Make a blob path safe for a bounded log line.

    Secret-shaped spans are redacted first (see :func:`_redact_path`),
    since a file name can itself be the sensitive value. Surrogates from
    non-UTF-8 names become ``?`` so printing never raises
    ``UnicodeEncodeError``; valid UTF-8 (including non-ASCII) passes
    through; newlines become ``\\n`` so one path cannot inject extra lines;
    other control characters (including ESC) are escaped so the path cannot
    drive the terminal; and the result is cut to ``_MAX_DISPLAY_PATH``
    characters, since file names are PR-controlled data.
    """
    safe = _redact_path(path).encode("utf-8", errors="replace").decode("utf-8")
    out = []
    for char in safe:
        if char == "\n":
            out.append("\\n")
        elif char == "\r":
            out.append("\\r")
        elif char == "\t" or char.isprintable():
            out.append(char)
        elif ord(char) < 256:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(f"\\u{ord(char):04x}")
    return "".join(out)[:_MAX_DISPLAY_PATH]


# Printed paths are cut here (see _display), and total stdout is capped in
# main(): a hostile tree cannot flood the workflow log, while the problem
# and suppression counts stay exact.
_MAX_DISPLAY_PATH = 200
# The stdout budget counts encoded UTF-8 bytes, not characters: multibyte
# names would otherwise multiply the byte count past the cap.
_MAX_LOG_BYTES = 100_000
_TRUNCATION_MARKER = (
    "hygiene-scan: ... output truncated "
    f"(over {_MAX_LOG_BYTES} bytes; counts above stay exact)"
)
_MARKER_BYTES = len(_TRUNCATION_MARKER.encode("utf-8")) + 1


def _print_capped(lines: list[str]) -> None:
    """Print ``lines`` up to a total byte budget, then stop with a marker.

    A line prints only if it and the marker still fit: the marker is
    reserved space, so the total with the marker never exceeds the budget.
    """
    shown = 0
    for line in lines:
        if shown + len(line.encode("utf-8")) + 1 + _MARKER_BYTES > _MAX_LOG_BYTES:
            print(_TRUNCATION_MARKER)
            return
        print(line)
        shown += len(line.encode("utf-8")) + 1


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
) -> list[tuple[str, int, str]]:
    """List the regular-blob entries in the tree at ``sha``.

    Returns ``(blob_sha, size, path)`` for regular blobs. Any symlink entry
    (mode 120000) or gitlink entry (mode 160000) anywhere in the tree fails
    the check: symlinks and submodules are not allowed in this repo. A link
    can smuggle an out-of-tree path (``..`` past a submodule, a repeated
    alias) past per-entry scanning, so resolving them is refused outright
    instead of attempted.

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
            # Symlinks and submodules are not allowed in this repo: a link
            # can smuggle an out-of-tree path past per-entry scanning (see
            # the docstring), so any entry of either kind fails the check.
            if mode == "120000" or objtype == "commit":
                raise FailClosed(
                    f"symlinks and submodules are not allowed in this repo "
                    f"({_display(path)}); refusing to scan"
                )
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
    return files


def read_blobs(
    blobs: list[tuple[str, int, str]], *, cwd: str, start: float | None = None
) -> list[bytes]:
    """Read every blob's bytes in one ``git cat-file --batch`` session.

    The batch output is walked with a cursor: each header is found from the
    current position and each blob is copied exactly once. Slicing the
    unconsumed suffix per object re-copies the remaining buffer every time,
    which is quadratic over many blobs. When ``start`` is given (the scan's
    deadline epoch), the deadline is also checked per blob.
    """
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
    data = proc.stdout
    pos = 0
    for sha, size, path in blobs:
        if start is not None:
            _check_deadline(start, "reading the blobs")
        end = data.find(b"\n", pos)
        if end == -1:
            raise FailClosed(
                "could not parse git cat-file --batch output; refusing to scan"
            )
        try:
            found, objtype, found_size = data[pos:end].decode("ascii").split(" ")
            found_size = int(found_size)
        except ValueError as exc:
            raise FailClosed(
                "could not parse git cat-file --batch output; refusing to scan"
            ) from exc
        if found != sha or objtype != "blob" or found_size != size:
            raise FailClosed(f"blob {_display(path)} changed mid-read; refusing to scan")
        pos = end + 1
        contents.append(data[pos : pos + size])
        pos += size
        if data[pos : pos + 1] != b"\n":
            raise FailClosed("could not parse git cat-file --batch output; refusing to scan")
        pos += 1
    return contents


def scan_git_tree(
    sha: str, *, cwd: str
) -> tuple[list[str], list[str], int, int]:
    """Scan every file in the tree at ``sha``. Returns ``(problems,
    suppressions, files_scanned, bytes_scanned)``; problems are display
    lines of the form ``path: problem`` and suppressions of the form
    ``path:lineno`` (never the line content: it may hold the suppressed
    secret).

    Any symlink (mode 120000) or gitlink (mode 160000) in the tree fails
    the check outright (see :func:`list_tree_blobs`): links are never
    resolved, so there is no link target to scan and no submodule to skip.
    """
    start = time.monotonic()
    _check_deadline(start, "starting")
    files = list_tree_blobs(sha, cwd=cwd)
    _check_deadline(start, "listing the tree")
    contents = read_blobs(files, cwd=cwd, start=start)
    _check_deadline(start, "reading the blobs")
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
            # The path is escaped and bounded; the line content never is.
            suppressions.append(f"{_display(path)}:{lineno}")

    for (_, size, path), data in zip(files, contents):
        check(path, data, size)
    # A blob may have run past the deadline while it scanned: never report
    # success on an expired deadline. (A step-level timeout-minutes in
    # ci-guard.yml is the hard bound; this is the in-process one.)
    _check_deadline(start, "finishing the scan")
    return problems, suppressions, len(files), scanned_bytes


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
        problems, suppressions, files_scanned, bytes_scanned = scan_git_tree(
            sha, cwd=cwd
        )
    except FailClosed as exc:
        print(f"::error::{escape_data(f'hygiene-scan: fail closed: {exc}')}")
        return 2
    lines = [
        f"hygiene-scan: scanned {files_scanned} file(s), {bytes_scanned} byte(s)"
    ]
    shown = problems[:MAX_LISTED_PROBLEMS]
    for problem in shown:
        lines.append(f"::error::{escape_data(f'hygiene-scan: {problem}')}")
    if len(problems) > len(shown):
        more = len(problems) - len(shown)
        lines.append(
            f"::error::{escape_data(f'hygiene-scan: ... and {more} more problem(s)')}"
        )
    lines.append(f"hygiene-scan: {len(suppressions)} line(s) carry {SUPPRESS!r}:")
    for line in suppressions[:MAX_LISTED_SUPPRESSIONS]:
        lines.append(f"hygiene-scan: suppression {line}")
    if len(suppressions) > MAX_LISTED_SUPPRESSIONS:
        more = len(suppressions) - MAX_LISTED_SUPPRESSIONS
        lines.append(f"hygiene-scan: ... and {more} more suppression(s)")
    if not suppressions:
        lines.append("hygiene-scan: no suppression lines")
    if problems:
        lines.append(
            f"::error::{escape_data(f'hygiene-scan: {len(problems)} problem(s) found')}"
        )
        _print_capped(lines)
        return 1
    lines.append("hygiene-scan: clean")
    _print_capped(lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

