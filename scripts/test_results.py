#!/usr/bin/env python3
"""Parse pytest JUnit XML and render test-result summaries (issue #11).

Two uses:

- In ``.github/workflows/ci.yml``: ``summary --junit junit.xml`` prints a
  Markdown table for the job summary page (``$GITHUB_STEP_SUMMARY``). A bad
  file prints a one-line notice and still exits 0, so the summary step never
  fails the job.
- In ``scripts/publish_evidence.py`` (main's code): ``parse_junit`` and
  ``render_comment_line`` build the one ``**Tests:**`` line of the evidence
  comment from the jobs API conclusions plus the per-version XML files.

Trust model: everything this module parses comes from PR-produced CI, so it
is untrusted. ``parse_junit`` refuses absurd input before parsing (size cap,
strict UTF-8 decoding with any other encoding refused, ``DOCTYPE``/``ENTITY``
scan on the decoded text), counts XML elements, validates every number, and
never echoes file content in its errors. Failure messages and tracebacks are
never extracted: only ``testcase`` tag structure and attribute names feed
the output, and failing names go through a strict allow-list (anything else
is counted as "not shown", never printed).
"""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

# A JUnit file for this suite is tens of KiB. Anything near this cap is a
# broken builder or an attempt to slow the publisher.
MAX_JUNIT_BYTES = 5_000_000

# Element cap against billion-laughs-style expansion (no entity parsing
# happens, but a flat million-element file still costs time and memory).
MAX_ELEMENTS = 50_000

# CI sets the suite name via ``-o junit_suite_name=python-<version>``, the
# version setup-python reports (expected to be exact, e.g. 3.14.1; a bare
# ``3.x`` would fail this check and degrade to "no valid results file").
VERSION_RE = re.compile(r"python-3\.\d{1,2}(\.\d{1,3})?")

# Failing test names that reach the comment. Letters, digits, and the small
# set of separators a ``classname::name`` value needs; at most 200 chars.
# Anything else (Markdown, HTML, backticks, newlines, huge blobs) is counted
# as hidden and never printed.
NAME_RE = re.compile(r"[A-Za-z0-9_.:/,\-=\[\]=]+")
MAX_NAME_LEN = 200

# At most this many failing names go in the comment line; the rest become
# "and N more".
MAX_NAMES = 20

# Expected leading bytes of a UTF-8 document. pytest always writes UTF-8
# JUnit XML, so a UTF-8 BOM is stripped but any other encoding is refused.
UTF8_BOM = b"\xef\xbb\xbf"

# Worst-conclusion ranking for duplicate same-version jobs. A PR controls its
# own ci.yml, so it could add a same-named job; the worst conclusion wins so
# a failure can never be masked, whichever order the jobs API lists them in.
# Any non-success beats success, and failure beats everything else.
CONCLUSION_SEVERITY = {
    "success": 0,
    "skipped": 1,
    "neutral": 2,
    "stale": 3,
    "cancelled": 4,
    "action_required": 5,
    "timed_out": 6,
    "failure": 7,
}


def worst_conclusion(conclusions: list) -> str:
    """The worst of several conclusions for one version (never masked)."""
    if not conclusions:
        raise ValueError("no conclusions to compare")
    return max(conclusions, key=lambda c: (CONCLUSION_SEVERITY.get(c, 4), c))


def _fail(reason: str) -> ValueError:
    # Fixed strings only: never interpolate file content into errors.
    return ValueError(f"bad junit xml: {reason}")


def _non_negative_int(suites: list[ET.Element], attr: str) -> int:
    total = 0
    for suite in suites:
        raw = suite.get(attr, "0")
        if not re.fullmatch(r"\d+", raw or ""):
            raise _fail(f"{attr} is not a non-negative integer")
        total += int(raw)
    return total


def parse_junit(data: bytes) -> dict:
    """Parse JUnit XML bytes into counts and failing names.

    Returns ``python``, ``tests``, ``failures``, ``errors``, ``skipped``,
    ``passed``, ``failed_names`` (allow-listed ``classname::name`` values)
    and ``hidden`` (failing names withheld by the allow-list). Raises
    ``ValueError`` with a fixed short message on any bad input.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_JUNIT_BYTES:
        raise _fail("input too large")
    raw = bytes(data)
    # UTF-8 only: pytest always writes UTF-8 JUnit XML. Strip an optional
    # UTF-8 BOM, then decode strictly. A raw-byte markup scan cannot see
    # through another encoding (UTF-16/32 spell "<!DOCTYPE" with NUL bytes),
    # and the XML parser would sniff that encoding and parse it anyway — so
    # anything that is not UTF-8 is refused here, before any parsing. NUL is
    # illegal in XML 1.0, so its presence always means a non-UTF-8 encoding
    # trying to smuggle markup past the text scan below.
    if raw.startswith(UTF8_BOM):
        raw = raw[len(UTF8_BOM):]
    if b"\x00" in raw:
        raise _fail("input is not utf-8")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _fail("input is not utf-8") from None
    declared = re.search(
        r"<\?xml\s[^>]*?encoding\s*=\s*[\"']([^\"']+)[\"']",
        text[:2000],
        re.IGNORECASE | re.DOTALL,
    )
    if declared is not None and declared.group(1).lower() != "utf-8":
        raise _fail("unsupported xml encoding")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise _fail("document type declarations are not allowed")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, RecursionError) as exc:
        raise _fail(f"not well-formed xml ({type(exc).__name__})") from exc
    if root.tag == "testsuites":
        suites = list(root.findall("testsuite"))
        if not suites:
            raise _fail("no testsuite found")
    elif root.tag == "testsuite":
        suites = [root]
    else:
        raise _fail("root is not testsuite or testsuites")
    count = sum(1 for _ in root.iter())
    if count > MAX_ELEMENTS:
        raise _fail("too many xml elements")
    versions = {suite.get("name") for suite in suites}
    if len(versions) != 1:
        raise _fail("testsuite names disagree")
    version = versions.pop()
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise _fail("bad testsuite name")
    python = version.removeprefix("python-")
    tests = _non_negative_int(suites, "tests")
    failures = _non_negative_int(suites, "failures")
    errors = _non_negative_int(suites, "errors")
    skipped = _non_negative_int(suites, "skipped")
    failed_names: list[str] = []
    hidden = 0
    for suite in suites:
        for case in suite.iter("testcase"):
            if case.find("failure") is None and case.find("error") is None:
                continue
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            rendered = f"{classname}::{name}"
            if len(rendered) <= MAX_NAME_LEN and NAME_RE.fullmatch(rendered):
                failed_names.append(rendered)
            else:
                hidden += 1
    passed = max(0, tests - failures - errors - skipped)
    return {
        "python": python,
        "tests": tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "passed": passed,
        "failed_names": failed_names,
        "hidden": hidden,
    }


def code_span(text: str) -> str:
    # Same technique as pr_evidence.code_span: grow the backticks past any
    # backticks the value holds, so nothing in it renders as Markdown.
    ticks = "`"
    while ticks in text:
        ticks += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def render_job_summary(parsed: dict) -> str:
    """A Markdown table for the CI job summary page."""
    lines = [
        "| Python | passed | failed | errors | skipped |",
        "|--------|--------|--------|--------|---------|",
        f"| {parsed['python']} | {parsed['passed']} | {parsed['failures']} "
        f"| {parsed['errors']} | {parsed['skipped']} |",
        "",
    ]
    if parsed["failed_names"]:
        lines.append("Failing:")
        lines.append("")
        for name in parsed["failed_names"]:
            lines.append(f"- {code_span(name)}")
        lines.append("")
    if parsed["hidden"]:
        lines.append(f"{parsed['hidden']} failing name(s) not shown.")
        lines.append("")
    return "\n".join(lines)


def _version_matches(short: str, full: str) -> bool:
    # The "3.x" job runs the newest Python, so any 3.x full version fits it;
    # a pinned job like "3.9" needs its own full version ("3.9" or "3.9.25").
    if short == "3.x":
        return full.startswith("3.")
    return full == short or full.startswith(short + ".")


def _is_valid_result(short: str, result: object) -> bool:
    if not (
        isinstance(result, dict)
        and isinstance(result.get("python"), str)
        and _version_matches(short, result["python"])
        and all(
            isinstance(result.get(key), int)
            for key in ("tests", "passed", "failures", "errors", "skipped")
        )
        and isinstance(result.get("failed_names"), list)
        and all(isinstance(n, str) for n in result["failed_names"])
        and isinstance(result.get("hidden"), int)
    ):
        return False
    # The API conclusion decides pass/fail, so inconsistent counts carry no
    # signal — only noise. Degrade rather than report numbers that add up
    # to more than the suite ran. (Missing count attributes already default
    # to 0 in the parser; that stays: it only moves the cosmetic counts.)
    parts = result.get("failures", 0) + result.get("errors", 0)
    parts += result.get("skipped", 0)
    return parts <= result.get("tests", -1)


def render_comment_line(
    jobs: list[dict] | None, results: dict, include_names: bool = True
) -> str:
    """The one evidence-comment line from validated jobs plus parsed results.

    ``jobs`` is a list of ``{"version": "3.9", "conclusion": "success"}``
    (``None`` when the jobs API data is missing or invalid); ``results``
    maps each short version to its parsed dict, or to anything else when
    that version has no valid results file. The API conclusion decides
    pass/fail; the XML only supplies counts and names, which are
    author-attested (reported by the PR's own CI; see D21).
    At most 20 failing names appear; the rest become "and N more". With
    ``include_names=False`` the failing names are dropped (counts and
    verdicts stay) so an over-budget comment can shrink the line instead of
    refusing the publish. Malformed job entries are skipped, so this never
    raises on unvalidated input.
    """
    if jobs is None or not isinstance(jobs, list):
        return "**Tests:** results unavailable."
    if not isinstance(results, dict):
        results = {}
    matching = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        version = job.get("version")
        conclusion = job.get("conclusion")
        if (
            not isinstance(version, str)
            or re.fullmatch(r"3\.(\d{1,2}|x)", version) is None
            or not isinstance(conclusion, str)
        ):
            continue
        matching.append({"version": version, "conclusion": conclusion})
    versions = sorted(
        {job["version"] for job in matching},
        key=lambda v: (v == "3.x", [int(p) for p in v.split(".")[1:] if p != "x"]),
    )
    if not versions:
        return "**Tests:** no test jobs found."
    remaining = MAX_NAMES if include_names else 0
    segments: list[str] = []
    passed_groups: dict[tuple[int, int], list[str]] = {}
    for short in versions:
        # A PR controls its own ci.yml, so it could add a same-named job;
        # the worst conclusion wins so a failure can never be masked,
        # whichever order the jobs API lists the duplicates in.
        conclusion = worst_conclusion(
            [job["conclusion"] for job in matching if job["version"] == short]
        )
        result = results.get(short)
        if not _is_valid_result(short, result):
            if conclusion == "success":
                segments.append(f"Python {short}: passed; no valid results file")
            else:
                segments.append(
                    f"Python {short}: job {conclusion}; no valid results file"
                )
            continue
        assert isinstance(result, dict)
        if conclusion == "success":
            key = (result["passed"], result["skipped"])
            passed_groups.setdefault(key, []).append(result["python"])
            continue
        # Only failure and timed_out mean the tests actually failed; every
        # other conclusion is reported as what it is, never as "failed".
        if conclusion in ("failure", "timed_out"):
            verdict = "**failed**"
        else:
            verdict = f"job {conclusion}"
        total = result["failures"] + result["errors"]
        names = result["failed_names"]
        known = len(names) + result["hidden"]
        headline = max(total, known)
        shown = names[:remaining]
        remaining -= len(shown)
        if headline and shown:
            text = (
                f"{verdict} on {result['python']} ({headline} failing: "
                + ", ".join(code_span(n) for n in shown)
            )
            if headline - len(shown) > 0:
                text += f", and {headline - len(shown)} more"
            text += ")"
        elif headline:
            text = f"{verdict} on {result['python']} ({headline} failing)"
        else:
            text = (
                f"{verdict} on {result['python']} "
                f"(job {conclusion}; XML reports {result['tests']} passed)"
            )
        segments.append(text)
    for (passed, skipped), fulls in passed_groups.items():
        text = f"{passed} passed on {' and '.join(fulls)}"
        if skipped:
            text += f", {skipped} skipped"
        segments.append(text)
    return "**Tests:** " + "; ".join(segments) + "."


def cmd_summary(args: argparse.Namespace) -> int:
    try:
        data = Path(args.junit).read_bytes()
    except OSError:
        print("Test results unavailable: cannot read the JUnit file.")
        return 0
    try:
        parsed = parse_junit(data)
    except ValueError as exc:
        print(f"Test results unavailable: {exc}")
        return 0
    print(render_job_summary(parsed), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    summary = sub.add_parser("summary", help="print the job-summary table")
    summary.add_argument("--junit", required=True)
    summary.set_defaults(func=cmd_summary)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
