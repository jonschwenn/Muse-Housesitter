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

Exit codes: 0 ok, 1 the artifact is invalid (skip the publish: no push, no
comment), 2 operational error (bad arguments, unreadable files).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pr_evidence  # noqa: E402  (main's code: this script runs from main only)
import ui_evidence  # noqa: E402

# The artifact's evidence.md is PR-produced text. Read it only as text, and
# refuse absurd sizes before the comment budget trims it: a giant file here
# is either a broken builder or an attempt to slow the publisher.
EVIDENCE_MAX_BYTES = 1_000_000


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


def render_comment(artifact: Path, repo: str, pr: int, head_sha: str) -> str:
    """Validate the artifact and build the final comment text.

    Raises PublishError when anything is invalid (fail closed: the caller
    pushes and posts nothing), ValueError when the caller's own
    repo/pr/head-sha arguments are malformed.
    """
    text = read_text_capped(artifact / "evidence.md", EVIDENCE_MAX_BYTES, "evidence.md")
    check_evidence_text(text)
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
    body = pr_evidence.append_ui_section(text, ui_dir, repo=repo, pr=pr, head_sha=head_sha)
    if failures and "Web UI checks failed" not in body:
        lines = body.split("\n")
        lines.insert(1, failure_notice(failures))
        lines.insert(2, "")
        body = "\n".join(lines)
    check_evidence_text(body)
    return body


def copy_pushable_files(ui_dir: Path, dest: Path) -> list[str]:
    """Copy exactly the validated PNGs and manifest. Returns file names.

    The allow-list is re-checked here at copy time (defense in depth: the
    workflow's push step never globs the untrusted directory itself).
    """
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in sorted(ui_dir.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            continue
        name = entry.name
        if name == ui_evidence.MANIFEST_NAME or ui_evidence.CAPTURE_RE.fullmatch(name):
            shutil.copyfile(entry, dest / name)
            copied.append(name)
    manifest_names: set[str] = set()
    data = json.loads((dest / ui_evidence.MANIFEST_NAME).read_text(encoding="utf-8"))
    captures = data.get("captures") if isinstance(data, dict) else None
    if not isinstance(captures, list) or not all(isinstance(c, dict) for c in captures):
        raise PublishError("manifest must be an object with a list of capture objects")
    for capture in captures:
        for key in ("base", "head", "diff"):
            value = capture.get(key)
            if isinstance(value, str):
                manifest_names.add(value)
    unlisted = [n for n in copied if n != ui_evidence.MANIFEST_NAME and n not in manifest_names]
    if unlisted:
        raise PublishError(f"unlisted files would be pushed: {', '.join(sorted(unlisted))}")
    return copied


def cmd_render(args: argparse.Namespace) -> int:
    artifact = Path(args.artifact_dir)
    out = Path(args.out_dir)
    try:
        body = render_comment(artifact, args.repo, args.pr, args.head_sha)
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
