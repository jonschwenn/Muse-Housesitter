#!/usr/bin/env python3
"""Privileged evidence publisher helper (docs/DECISIONS.md D16).

Runs in ``.github/workflows/evidence-publish.yml`` from the ``main``
checkout only — never from PR code. The downloaded evidence artifact is
fully untrusted (a fork PR's ``ci.yml`` is PR-authored, and a same-repo PR
can rewrite it): nothing from the artifact is executed, sourced, or
trusted. PNGs and the manifest are validated with main's
``ui_evidence.validate_artifact`` before anything is pushed or posted, and
the Web UI comment section is rebuilt from the validated manifest with
main's ``pr_evidence`` renderer, ignoring any pre-rendered markdown in the
artifact.

Subcommands (called by the workflow, in order across its jobs):

    render --artifact-dir ART --out-dir OUT --repo OWNER/NAME --pr 12 \\
        --head-sha <40 hex>
        Validate the artifact and build the final comment (with the evidence
        push SHA still a placeholder) at OUT/comment.md, plus the pushable
        files at OUT/files/ (validated PNGs and the manifest only).

    substitute --comment OUT/comment.md --sha <push sha>
        Pin the placeholder to the evidence push SHA. Validates the SHA.

Exit codes: 0 ok; 1 the artifact is invalid, including unreadable or
malformed artifact files (render: skip the publish, no push, no comment);
2 bad caller arguments (render), or an unreadable or invalid comment file
or SHA (substitute).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_evidence  # noqa: E402  (main's code: this script runs from main only)
import test_results  # noqa: E402  (main's code: validates PR-produced XML)
import ui_evidence  # noqa: E402

# The artifact's evidence.md is PR-produced text. Read it only as text, and
# refuse absurd sizes before the comment budget trims it: a giant file here
# is either a broken builder or an attempt to slow the publisher.
EVIDENCE_MAX_BYTES = 1_000_000

# Test results (issue #11, decision D21 option A): the CI run's JUnit files
# and jobs JSON are PR-produced too, but a bad test-results file degrades
# only the Tests line, never the whole publish. Bad UI artifacts keep their
# existing behaviour (refuse the whole publish).
TEST_RESULTS_DIR_RE = re.compile(r"test-results-(3\.x|3\.\d{1,2})")
MAX_TEST_RESULT_DIRS = 4
JOBS_MAX_BYTES = 200_000
MAX_JOBS = 100
JOB_NAME_RE = re.compile(r"test \((3\.x|3\.\d{1,2})\)")
JOB_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        "stale",
    }
)
TEST_HEADING = "## Before / after evidence"


class PublishError(Exception):
    """The artifact failed validation: skip the publish, post nothing."""


def read_text_capped(path: Path, cap: int, what: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PublishError(f"cannot read {what}: {exc}") from exc
    if size > cap:
        raise PublishError(f"{what} is {size} bytes, over the {cap} cap")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise PublishError(f"cannot read {what}: {exc}") from exc


def check_evidence_text(text: str) -> None:
    """Refuse a comment body that lacks the marker or exceeds the budget."""
    if not text.startswith(pr_evidence.MARKER):
        raise PublishError("evidence.md does not start with the marker")
    if len(text.encode()) > pr_evidence.MAX_COMMENT_BYTES:
        raise PublishError("evidence.md is too large for a comment")


def failure_notice(count: int) -> str:
    return (
        f"**Web UI checks failed:** {count} capture(s) report "
        "problems; see the Web UI section below."
    )


def has_manifest(ui_dir: Path) -> bool:
    """Whether the artifact ships UI evidence at all."""
    manifest = ui_dir / ui_evidence.MANIFEST_NAME
    return manifest.is_file() and not manifest.is_symlink()


def load_json_docs(path: Path, cap: int) -> list | None:
    """Read one JSON document, or several concatenated ones (``gh api
    --paginate`` prints one response body per page). Returns None when the
    file is missing, oversized, undecodable, or unparseable: the caller
    degrades the Tests line instead of refusing the publish."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > cap:
        return None
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, ValueError):
        return None
    try:
        return [json.loads(raw)]
    except RecursionError:
        return None
    except ValueError:
        pass
    docs = []
    decoder = json.JSONDecoder()
    index = 0
    try:
        while index < len(raw):
            while index < len(raw) and raw[index].isspace():
                index += 1
            if index >= len(raw):
                break
            doc, end = decoder.raw_decode(raw, index)
            docs.append(doc)
            index = end
    except (ValueError, RecursionError):
        return None
    return docs or None


def validate_jobs(docs: list | None) -> list[dict] | None:
    """Keep the CI test jobs from a jobs-API response, or None when the data
    is missing or invalid. Merges concatenated pages; refuses truncated
    listings, oversized ones, bad shapes, and conclusions outside the fixed
    set. Jobs whose names are not CI test jobs are ignored."""
    if not docs:
        return None
    merged: list = []
    totals: list[int] = []
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), list):
            return None
        items = doc["jobs"]
        if len(items) > MAX_JOBS:
            return None
        merged.extend(items)
        if isinstance(doc.get("total_count"), int):
            totals.append(doc["total_count"])
    if len(merged) > MAX_JOBS:
        return None
    if totals and max(totals) > len(merged):
        # The listing is truncated: a test job could be hiding off-page.
        return None
    kept: dict[str, str] = {}
    for job in merged:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            return None
        match = JOB_NAME_RE.fullmatch(job["name"])
        if match is None:
            continue
        if job.get("conclusion") not in JOB_CONCLUSIONS:
            return None
        version = match.group(1)
        if version in kept:
            # A PR controls its own ci.yml, so it could add a same-named
            # job; the worst conclusion wins so a failure can never be
            # masked, whichever order the jobs API lists them in.
            kept[version] = test_results.worst_conclusion(
                [kept[version], job["conclusion"]]
            )
        else:
            kept[version] = job["conclusion"]
    return [{"version": version, "conclusion": conclusion} for version, conclusion in kept.items()]


def load_test_results(root: Path | None) -> dict:
    """Read per-version JUnit files from a hostile directory. Returns a map
    of short version to parsed result or the string ``"invalid"``; anything
    unexpected (missing directory, stray entries, symlinks, too many
    versions) degrades to missing/invalid entries, never an exception."""
    results: dict = {}
    if root is None:
        return results
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return results
    matching = [e for e in entries if TEST_RESULTS_DIR_RE.fullmatch(e.name)]
    if len(matching) != len(entries) or len(matching) > MAX_TEST_RESULT_DIRS:
        return results
    for entry in matching:
        version = TEST_RESULTS_DIR_RE.fullmatch(entry.name).group(1)  # type: ignore[union-attr]
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            children = list(entry.iterdir())
        except OSError:
            results[version] = "invalid"
            continue
        if (
            len(children) != 1
            or children[0].name != "junit.xml"
            or children[0].is_symlink()
            or not children[0].is_file()
        ):
            results[version] = "invalid"
            continue
        try:
            # Size first: never read a huge PR-produced file into memory.
            if children[0].stat().st_size > test_results.MAX_JUNIT_BYTES:
                results[version] = "invalid"
                continue
            data = children[0].read_bytes()
        except OSError:
            results[version] = "invalid"
            continue
        try:
            results[version] = test_results.parse_junit(data)
        except ValueError:
            results[version] = "invalid"
    return results


def insert_test_line(text: str, line: str) -> str:
    """Insert the Tests line right after the evidence heading (or after the
    marker when the heading is absent), so the comment still publishes."""
    lines = text.split("\n")
    for index, value in enumerate(lines):
        if value == TEST_HEADING:
            lines.insert(index + 1, line)
            break
    else:
        lines.insert(1, line)
    return "\n".join(lines)


# Fixed short Tests line used only when even the shrunk line would push a
# near-cap comment over budget. Test data must never break the publish
# (decision D21 option A), so the line degrades instead of refusing it.
TEST_LINE_FALLBACK = "**Tests:** results not shown (comment size limit)."


def _test_inputs(
    test_results_dir: str | None, jobs_json: str | None
) -> tuple[list[dict] | None, dict]:
    """Validated jobs plus per-version results for the Tests line."""
    jobs = None
    if jobs_json is not None:
        jobs = validate_jobs(load_json_docs(Path(jobs_json), JOBS_MAX_BYTES))
    results = load_test_results(
        Path(test_results_dir) if test_results_dir is not None else None
    )
    return jobs, results


def fit_test_line(evidence_text: str, test_line: str, brief_line: str) -> str:
    """Insert the Tests line, shrinking it when the comment is over budget.

    Drops the failing names first (counts and verdicts stay), then falls
    back to a fixed short line, and leaves the line out entirely when even
    that doesn't fit. Only evidence that is already over budget on its own
    is refused by the caller; test data never refuses.
    """
    text = insert_test_line(evidence_text, test_line)
    if len(text.encode()) <= pr_evidence.MAX_COMMENT_BYTES:
        return text
    text = insert_test_line(evidence_text, brief_line)
    if len(text.encode()) <= pr_evidence.MAX_COMMENT_BYTES:
        return text
    text = insert_test_line(evidence_text, TEST_LINE_FALLBACK)
    if len(text.encode()) <= pr_evidence.MAX_COMMENT_BYTES:
        return text
    return evidence_text


def render_comment(
    artifact: Path,
    repo: str,
    pr: int,
    head_sha: str,
    test_results_dir: str | None = None,
    jobs_json: str | None = None,
) -> str:
    """Validate the artifact and build the final comment text.

    Raises PublishError when anything is invalid (fail closed: the caller
    pushes and posts nothing), ValueError when the caller's own
    repo/pr/head-sha arguments are malformed.
    """
    text = read_text_capped(artifact / "evidence.md", EVIDENCE_MAX_BYTES, "evidence.md")
    check_evidence_text(text)
    # Test data must never break the publish (decision D21 option A): after
    # inserting the Tests line, shrink it (drop names, then a fixed short
    # line) when the comment would go over budget. Only evidence that is
    # already over budget on its own refuses above.
    evidence_only = text
    if test_results_dir is not None or jobs_json is not None:
        jobs, results = _test_inputs(test_results_dir, jobs_json)
        text = fit_test_line(
            text,
            test_results.render_comment_line(jobs, results),
            test_results.render_comment_line(jobs, results, include_names=False),
        )
    ui_dir = artifact / "ui_out"
    manifest_path = ui_dir / ui_evidence.MANIFEST_NAME
    if os.path.lexists(manifest_path) and not has_manifest(ui_dir):
        # A symlink or directory in the manifest's place is a broken or
        # hostile artifact, not "no UI evidence": refuse, don't drop the
        # Web UI section quietly.
        raise PublishError(f"{ui_evidence.MANIFEST_NAME} is not a regular file")
    if not ui_dir.exists() or not has_manifest(ui_dir):
        # No manifest (the capture died before writing one): the text
        # comment still publishes. No web/ on either side is not this case:
        # that manifest has an empty captures list and gets a Web UI section.
        return text
    errors = ui_evidence.validate_artifact(ui_dir)
    if errors:
        raise PublishError("artifact validation failed: " + "; ".join(errors[:10]))
    try:
        manifest = pr_evidence.load_ui_manifest(ui_dir)
    except (OSError, ValueError, RecursionError) as exc:
        # The validator and the loader must agree, but if they ever drift
        # (or the file changes between the two reads), an artifact problem
        # is still an invalid artifact (exit 1), never a bad argument.
        # json.JSONDecodeError subclasses ValueError, so this covers a
        # corrupt manifest too. render_ui_section's ValueError (bad
        # repo/pr/head-sha) is deliberately left to propagate as exit 2.
        raise PublishError(f"artifact validation failed: {exc}") from exc
    if manifest is None:
        return text
    _, _, failures = pr_evidence.render_ui_section(manifest, repo, pr, head_sha)

    def with_ui(base: str) -> str:
        body = pr_evidence.append_ui_section(
            base, ui_dir, repo=repo, pr=pr, head_sha=head_sha
        )
        if failures and "Web UI checks failed" not in body:
            lines = body.split("\n")
            lines.insert(1, failure_notice(failures))
            lines.insert(2, "")
            body = "\n".join(lines)
        return body

    body = with_ui(text)
    if text != evidence_only and len(body.encode()) > pr_evidence.MAX_COMMENT_BYTES:
        # The Tests line is what tipped it over: drop it rather than let
        # test data refuse the publish (D21 option A).
        body = with_ui(evidence_only)
    check_evidence_text(body)
    return body


def copy_pushable_files(ui_dir: Path, dest: Path) -> list[str]:
    """Copy exactly the pictured PNGs and manifest. Returns file names.

    The allow-list is re-checked here at copy time (defense in depth: the
    workflow's push step never globs the untrusted directory itself).
    What counts as pushable is decided by pr_evidence.select_ui_captures,
    the same function the comment renderer uses: each kept capture
    contributes exactly the files its details block references, so the push
    carries exactly what the full Web UI section pictures (under the
    table-only budget fallback, pushed images are not pictured). PNGs named only
    by dropped entries (or by forbidden fields of any entry) are skipped;
    a capture-named PNG the manifest does not mention anywhere is still a
    refusal, as before.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(
            (ui_dir / ui_evidence.MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, RecursionError) as exc:
        raise PublishError(f"manifest is unreadable: {exc}") from exc
    captures = data.get("captures") if isinstance(data, dict) else None
    if not isinstance(captures, list) or not all(isinstance(c, dict) for c in captures):
        raise PublishError("manifest must be an object with a list of capture objects")
    kept, _ = pr_evidence.select_ui_captures(data)
    referenced: set[str] = set()
    for _, files in kept:
        referenced.update(files)
    manifest_names: set[str] = set()
    for capture in captures:
        for key in ("base", "head", "diff"):
            value = capture.get(key)
            if isinstance(value, str):
                manifest_names.add(value)
    copied = []
    unlisted = []
    for entry in sorted(ui_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        name = entry.name
        if name == ui_evidence.MANIFEST_NAME:
            shutil.copyfile(entry, dest / name)
            copied.append(name)
        elif ui_evidence.CAPTURE_RE.fullmatch(name):
            if name in referenced:
                shutil.copyfile(entry, dest / name)
                copied.append(name)
            elif name in manifest_names:
                # Named only by a dropped entry or a forbidden field: leave
                # it behind so unreferenced bytes never reach the branch.
                continue
            else:
                unlisted.append(name)
    if unlisted:
        raise PublishError(
            f"unlisted files would be pushed: {', '.join(sorted(unlisted))}"
        )
    return copied


def cmd_render(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact_dir)
    out = Path(args.out_dir)
    try:
        body = render_comment(
            artifact,
            args.repo,
            args.pr,
            args.head_sha,
            test_results_dir=args.test_results_dir,
            jobs_json=args.jobs_json,
        )
    except PublishError as exc:
        print(f"publish_evidence: refusing to publish: {exc}", file=sys.stderr)
        return 1
    out.mkdir(parents=True, exist_ok=True)
    (out / "comment.md").write_text(body, encoding="utf-8")
    ui_dir = artifact / "ui_out"
    files = out / "files"
    if ui_dir.exists() and has_manifest(ui_dir):
        try:
            names = copy_pushable_files(ui_dir, files)
        except PublishError as exc:
            print(f"publish_evidence: refusing to publish: {exc}", file=sys.stderr)
            return 1
        pngs = [n for n in names if n.endswith(".png")]
        print(f"publish_evidence: rendered comment with {len(pngs)} PNG(s)")
    else:
        files.mkdir(parents=True, exist_ok=True)
        print("publish_evidence: rendered comment with no UI files")
    return 0


def cmd_substitute(args: argparse.Namespace) -> int:
    path = Path(args.comment)
    try:
        text = read_text_capped(path, EVIDENCE_MAX_BYTES, "comment.md")
    except PublishError as exc:
        print(f"publish_evidence: {exc}", file=sys.stderr)
        return 2
    try:
        text = ui_evidence.substitute_sha(text, args.sha)
    except ValueError as exc:
        print(f"publish_evidence: {exc}", file=sys.stderr)
        return 2
    path.write_text(text, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render", help="validate the artifact and build the comment")
    render.add_argument("--artifact-dir", required=True)
    render.add_argument("--out-dir", required=True)
    render.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    render.add_argument("--pr", required=True, type=int, help="pull request number")
    render.add_argument("--head-sha", required=True, help="head commit SHA, 40 hex")
    render.add_argument(
        "--test-results-dir",
        default=None,
        help="directory of test-results-* JUnit downloads (optional)",
    )
    render.add_argument(
        "--jobs-json",
        default=None,
        help="jobs-API response file for the run attempt (optional)",
    )
    render.set_defaults(func=cmd_render)
    subst = sub.add_parser("substitute", help="pin the placeholder to the push SHA")
    subst.add_argument("--comment", required=True)
    subst.add_argument("--sha", required=True)
    subst.set_defaults(func=cmd_substitute)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        # Malformed repo/pr/head-sha arguments (the renderers validate them).
        print(f"publish_evidence: bad argument: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
