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
SECRET_PATTERNS = [
    ("private key", re.compile("-----BEGIN [A-Z ]*" + "PRIVATE " + "KEY-----")),
    ("Muse SDK token", re.compile("mg" + r"st_[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}")),
    ("credentials in a URL", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
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
        re.compile(rf"""(?i)["']?{KEY_WORDS}["']?\s*[:=]\s*(?:"([^"\n]{{8,}})"|'([^'\n]{{8,}})')"""),
    ),
]
# YAML, INI, and env files also allow unquoted values. Not applied to Python,
# where an unquoted value is code (password = ctx.secret("unifi_password")).
UNQUOTED_ASSIGNMENT = re.compile(
    rf"""(?im)^[ \t]*(?:export[ \t]+)?["']?{KEY_WORDS}["']?[ \t]*[:=][ \t]*"""
    r"""([^\s"'#,{}\[\]][^\s#,]{7,})"""
)
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


def secret_problems(text: str, unquoted: bool = True) -> list[str]:
    found = []
    patterns = SECRET_PATTERNS + (
        [("credential assignment", UNQUOTED_ASSIGNMENT)] if unquoted else []
    )
    for label, pattern in patterns:
        for match in pattern.finditer(text):
            if PLANTED in match.group(0):
                continue
            value = next((g for g in match.groups() if g), None) if match.groups() else None
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
# Problem and suppression listings are capped so a hostile tree cannot flood
# the workflow log; the counts stay exact.
MAX_LISTED_PROBLEMS = 200
MAX_LISTED_SUPPRESSIONS = 1000
MAX_DISPLAY_LINE = 200


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
    """Make a blob path safe for a single log line."""
    return path.replace("\n", "\\n").replace("\r", "\\r")


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


def list_tree_blobs(sha: str, *, cwd: str) -> list[tuple[str, int, str]]:
    """List ``(blob_sha, size, path)`` for every blob in the tree at ``sha``.

    Submodules (commits inside the tree) are skipped, like the working-tree
    scan skips paths that are not files. Fails closed when the entry count,
    the total bytes, or any single blob exceeds the bounds above.
    """
    out = _git(
        [
            "-c",
            "core.quotePath=false",
            "ls-tree",
            "-r",
            "-z",
            "--format=%(objectmode) %(objecttype) %(objectname) %(objectsize)\t%(path)",
            sha,
        ],
        cwd=cwd,
    )
    raws = [entry for entry in out.split(b"\0") if entry]
    if len(raws) > MAX_TREE_ENTRIES:
        raise FailClosed(
            f"tree lists {len(raws)} entries (limit {MAX_TREE_ENTRIES}); refusing to scan"
        )
    blobs = []
    total = 0
    for raw in raws:
        meta, _, raw_path = raw.partition(b"\t")
        try:
            _, objtype, objsha, size = meta.decode("ascii").split(" ")
            size = int(size)
        except ValueError as exc:
            raise FailClosed("could not parse the tree listing; refusing to scan") from exc
        if objtype != "blob":
            continue  # a submodule punt: not a file, nothing to scan
        path = raw_path.decode("utf-8", errors="replace")
        if size > MAX_FILE_BYTES:
            raise FailClosed(
                f"blob {_display(path)} is {size} bytes (limit {MAX_FILE_BYTES}); refusing to scan"
            )
        blobs.append((objsha, size, path))
        total += size
    if total > MAX_TOTAL_BYTES:
        raise FailClosed(
            f"tree holds {total} bytes (limit {MAX_TOTAL_BYTES}); refusing to scan"
        )
    return blobs


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


def scan_git_tree(sha: str, *, cwd: str) -> tuple[list[str], list[str], int, int]:
    """Scan every blob in the tree at ``sha``. Returns ``(problems,
    suppressions, files_scanned, bytes_scanned)``; both lists are display
    lines of the form ``path: problem`` and ``path:lineno: line``."""
    blobs = list_tree_blobs(sha, cwd=cwd)
    contents = read_blobs(blobs, cwd=cwd)
    problems = []
    suppressions = []
    scanned_bytes = 0
    for (_, size, path), data in zip(blobs, contents):
        scanned_bytes += size
        text = decode_bytes(data)
        if text is None:
            continue
        for problem in scan_text(path, text):
            problems.append(f"{_display(path)}: {problem}")
        for lineno, line in suppression_lines(text):
            suppressions.append(f"{_display(path)}:{lineno}: {line[:MAX_DISPLAY_LINE]}")
    return problems, suppressions, len(blobs), scanned_bytes


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
        problems, suppressions, files_scanned, bytes_scanned = scan_git_tree(sha, cwd=cwd)
    except FailClosed as exc:
        print(f"::error::{escape_data(f'hygiene-scan: fail closed: {exc}')}")
        return 2
    print(f"hygiene-scan: scanned {files_scanned} file(s), {bytes_scanned} byte(s)")
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

