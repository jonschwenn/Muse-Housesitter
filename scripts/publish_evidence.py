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
malformed artifact files (render: write refusal.json with the fixed reason
code, skip the push, and let the workflow post the fixed notice instead of
the comment); 2 bad caller arguments (render), or an unreadable or invalid
comment file, SHA, or refusal file (substitute, refusal).
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

import evidence_checks  # noqa: E402  (main's code: this script runs from main only)
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
    """The artifact failed validation: skip the publish, post a fixed notice.

    ``reason`` is one of evidence_checks.REFUSAL_REASONS; cmd_render writes
    it to refusal.json so the workflow can post the fixed notice.
    """

    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.reason = reason


def read_text_capped(path: Path, cap: int, what: str, reason: str) -> str:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PublishError(f"cannot read {what}: {exc}", reason) from exc
    if size > cap:
        raise PublishError(f"{what} is {size} bytes, over the {cap} cap", "too_large")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise PublishError(f"cannot read {what}: {exc}", reason) from exc


def check_evidence_text(text: str) -> None:
    """Refuse a comment body that lacks the marker or exceeds the budget."""
    if not text.startswith(pr_evidence.MARKER):
        raise PublishError("comment does not start with the marker", "bad_schema")
    if len(text.encode()) > pr_evidence.MAX_COMMENT_BYTES:
        raise PublishError("comment is too large for a comment", "too_large")


def main_scenario_ids() -> set[str]:
    """Scenario ids in MAIN's list: the spec the results are checked against.

    The publisher runs from the main checkout, so this file is main's copy
    and these ids are trusted; the PR only supplies results claiming to
    match them (or marked new-in-this-PR).
    """
    path = Path(__file__).resolve().parent.parent / "tests" / "evidence" / "scenarios.json"
    try:
        return evidence_checks.load_known_scenario_ids(path)
    except (OSError, ValueError, KeyError) as exc:
        raise PublishError(f"cannot read main's scenarios list: {exc}", "missing") from exc


def read_results(artifact: Path) -> dict:
    """Read and parse the structured results (Decision 13, option A).

    The PR's markdown (evidence.md) is ignored entirely: the comment is
    rebuilt from this file, which is validated strictly before anything
    renders.
    """
    path = artifact / evidence_checks.RESULTS_FILENAME
    text = read_text_capped(path, evidence_checks.RESULTS_MAX_BYTES, "results.json", "missing")
    try:
        doc = json.loads(text)
    except RecursionError as exc:
        raise PublishError(f"results.json is nested too deeply: {exc}", "bad_schema") from exc
    except ValueError as exc:
        raise PublishError(f"results.json is not valid JSON: {exc}", "bad_schema") from exc
    return doc


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
    if not pr_evidence.UI_REPO_RE.fullmatch(repo or ""):
        raise ValueError(f"bad repo: {repo!r}")
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError(f"bad PR number: {pr!r}")
    if not pr_evidence.UI_SHA_RE.fullmatch(head_sha or ""):
        raise ValueError(f"bad head SHA: {head_sha!r}")
    results = read_results(artifact)
    errors = evidence_checks.validate_results(results, main_scenario_ids())
    if errors:
        raise PublishError(
            "results.json failed validation: " + "; ".join(errors[:10]), "bad_schema"
        )
    claimed = results.get("metadata", {}).get("head_sha")
    if claimed is not None and claimed != head_sha:
        raise PublishError(
            f"results.json is for head {claimed}, not the published run {head_sha}",
            "stale_head",
        )
    # The text section is rebuilt here from the validated results; the
    # PR-supplied markdown (evidence.md) is never read, let alone posted.
    text = pr_evidence.render_text_from_results(results, head_sha)
    # Test data must never break the publish (decision D21 option A): after
    # inserting the Tests line, shrink it (drop names, then a fixed short
    # line) when the comment would go over budget. Only text that is
    # already over budget on its own refuses below.
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
        raise PublishError(
            f"{ui_evidence.MANIFEST_NAME} is not a regular file", "bad_schema"
        )
    if not ui_dir.exists() or not has_manifest(ui_dir):
        # No manifest (the capture died before writing one): the text
        # comment still publishes. No web/ on either side is not this case:
        # that manifest has an empty captures list and gets a Web UI section.
        # The text is freshly rebuilt (not pre-checked like the old
        # evidence.md), so the budget check applies here too.
        check_evidence_text(text)
        return text
    errors = ui_evidence.validate_artifact(ui_dir)
    if errors:
        raise PublishError(
            "artifact validation failed: " + "; ".join(errors[:10]),
            evidence_checks.artifact_refusal_reason(errors),
        )
    png_errors = validate_png_files(ui_dir)
    if png_errors:
        raise PublishError(
            "screenshot validation failed: " + "; ".join(png_errors[:10]), "bad_png"
        )
    try:
        manifest = pr_evidence.load_ui_manifest(ui_dir)
    except (OSError, ValueError, RecursionError) as exc:
        # The validator and the loader must agree, but if they ever drift
        # (or the file changes between the two reads), an artifact problem
        # is still an invalid artifact (exit 1), never a bad argument.
        # json.JSONDecodeError subclasses ValueError, so this covers a
        # corrupt manifest too. render_ui_section's ValueError (bad
        # repo/pr/head-sha) is deliberately left to propagate as exit 2.
        raise PublishError(f"artifact validation failed: {exc}", "bad_schema") from exc
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


def validate_png_files(ui_dir: Path) -> list[str]:
    """Run the structural PNG check over every capture-named file.

    ``validate_artifact`` checks names, caps, and magic bytes; this parses
    the structure (S5: chunk lengths, CRC of every chunk, IHDR caps, IEND
    placement, bounded IDAT decompression). Render calls it before building
    the comment and copy calls it again before pushing (defense in depth:
    the directory is re-read, never trusted from the earlier pass).
    """
    problems: list[str] = []
    try:
        entries = sorted(ui_dir.iterdir())
    except OSError as exc:
        raise PublishError(f"cannot list UI directory: {exc}", "missing") from exc
    for entry in entries:
        if entry.name == ui_evidence.MANIFEST_NAME:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        if not ui_evidence.CAPTURE_RE.fullmatch(entry.name):
            continue  # validate_artifact already refuses these
        try:
            data = entry.read_bytes()
        except OSError as exc:
            raise PublishError(f"cannot read {entry.name}: {exc}", "missing") from exc
        for problem in evidence_checks.validate_png(data):
            problems.append(f"{entry.name}: {problem}")
    return problems


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
        raise PublishError(f"manifest is unreadable: {exc}", "bad_schema") from exc
    captures = data.get("captures") if isinstance(data, dict) else None
    if not isinstance(captures, list) or not all(isinstance(c, dict) for c in captures):
        raise PublishError(
            "manifest must be an object with a list of capture objects", "bad_schema"
        )
    kept, _ = pr_evidence.select_ui_captures(data)
    png_errors = validate_png_files(ui_dir)
    if png_errors:
        raise PublishError(
            "screenshot validation failed: " + "; ".join(png_errors[:10]), "bad_png"
        )
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
            f"unlisted files would be pushed: {', '.join(sorted(unlisted))}", "bad_schema"
        )
    return copied


REFUSAL_FILENAME = "refusal.json"


def _write_refusal(out: Path, reason: str) -> None:
    """Record the fixed refusal reason so the workflow can post the notice."""
    out.mkdir(parents=True, exist_ok=True)
    (out / REFUSAL_FILENAME).write_text(
        json.dumps({"reason": reason}) + "\n", encoding="utf-8"
    )


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
        # Decision 4, option B: never go silent. Record the fixed reason
        # code so the workflow can post the fixed notice; the message above
        # stays in the job log for debugging and never reaches the comment.
        _write_refusal(out, exc.reason)
        return 1
    except UnicodeEncodeError as exc:
        # Artifact text the schema missed (a lone surrogate in a manifest
        # string the UI validator does not encode-check): still hostile
        # data, never a caller bug. UnicodeEncodeError subclasses
        # ValueError, so this must come before the re-raise below.
        print(f"publish_evidence: refusing to publish: {exc}", file=sys.stderr)
        _write_refusal(out, "render_error")
        return 1
    except ValueError:
        # Malformed repo/pr/head-sha arguments: a caller bug, still exit 2.
        raise
    except Exception as exc:
        # Any other surprise while handling artifact data (a KeyError past
        # a validator gap, a TypeError on a hostile manifest type, deep
        # nesting): refuse with a fixed reason instead of dying silent.
        print(f"publish_evidence: refusing to publish: {exc}", file=sys.stderr)
        _write_refusal(out, "render_error")
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
            _write_refusal(out, exc.reason)
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
        text = read_text_capped(path, EVIDENCE_MAX_BYTES, "comment.md", "bad_schema")
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


def cmd_refusal(args: argparse.Namespace) -> int:
    """Print the fixed refusal notice for a refusal.json file (exit 0).

    The workflow posts this as the evidence comment when render refused.
    The reason is read from the file and validated here in Python (never
    interpolated from the shell), and the notice itself is main's fixed
    text: no PR-supplied content can reach the comment through it.
    """
    try:
        doc = json.loads(Path(args.reason_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"publish_evidence: cannot read refusal file: {exc}", file=sys.stderr)
        return 2
    if not isinstance(doc, dict):
        print("publish_evidence: refusal file must be an object", file=sys.stderr)
        return 2
    try:
        print(evidence_checks.refusal_notice(doc.get("reason")), end="")
    except ValueError as exc:
        print(f"publish_evidence: {exc}", file=sys.stderr)
        return 2
    return 0


# Comment-job decision (review round 1, finding 4) -------------------------------
#
# The comment job must post even when the build or the push failed: a failed
# push used to skip it, and a build that died before uploading left its
# download failing. This decision lives here in main's Python so it is
# unit-tested; the workflow only carries out the printed word. Only the
# fixed words below ever print: job results are GitHub-provided, never PR
# content. A stale head is not a failure: the workflow checks freshness
# separately and writes nothing then.

COMMENT_ACTIONS = ("comment", "refusal-notice", "publish-failed", "nothing")


def decide_comment_action(
    build_result: str, push_result: str, has_refusal: bool, has_comment: bool
) -> str:
    """What the comment job should post.

    ``build_result``/``push_result`` are GitHub job results (success,
    failure, cancelled, skipped); ``has_refusal``/``has_comment`` say
    whether the (tolerant) download produced those files. Returns one of
    COMMENT_ACTIONS: post the rendered comment, post the render refusal,
    post the fixed publish_failed notice, or post nothing.
    """
    if build_result not in ("success", "failure"):
        return "nothing"
    if push_result not in ("success", "failure", "skipped"):
        return "nothing"
    if (
        build_result == "success"
        and push_result == "success"
        and has_comment
        and not has_refusal
    ):
        return "comment"
    if build_result == "failure" and has_refusal:
        return "refusal-notice"
    return "publish-failed"


def cmd_decide_comment(args: argparse.Namespace) -> int:
    print(
        decide_comment_action(
            args.build_result, args.push_result, args.has_refusal, args.has_comment
        )
    )
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
    refusal = sub.add_parser(
        "refusal", help="print the fixed refusal notice for a refusal.json file"
    )
    refusal.add_argument("--reason-file", required=True)
    refusal.set_defaults(func=cmd_refusal)
    decide = sub.add_parser(
        "decide-comment", help="print what the comment job should post"
    )
    decide.add_argument("--build-result", required=True)
    decide.add_argument("--push-result", required=True)
    decide.add_argument("--has-refusal", action="store_true")
    decide.add_argument("--has-comment", action="store_true")
    decide.set_defaults(func=cmd_decide_comment)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        # Malformed repo/pr/head-sha arguments (the renderers validate them).
        print(f"publish_evidence: bad argument: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
