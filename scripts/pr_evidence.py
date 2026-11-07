#!/usr/bin/env python3
"""Build the before/after evidence comment for a PR (docs/REVIEW.md, "Evidence").

Runs every scenario in the head tree's scenarios file against two source
trees, the PR's base and its head, and writes one Markdown comment body:
a summary table, then a diff for every scenario whose output changed.

Safety: each run gets a fresh, empty HOME and a minimal environment (the same
variables the Muse SDK gives drop-in commands), so no real config, secret, or
saved snapshot can be read. The script refuses to start if it looks like it
is running on a machine with a real Housesitter setup.

Usage:
    python scripts/pr_evidence.py --base-tree BASE --head-tree HEAD --out comment.md
    python scripts/pr_evidence.py --base-tree BASE --head-tree HEAD --out comment.md \
        --ui-artifacts UI --repo OWNER/NAME --pr 12 --head-sha <40 hex>

The Web UI section is built here from the artifact's ui_manifest.json, with
every value re-validated; pre-rendered markdown in the artifact is ignored.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "<!-- housesitter-evidence -->"
SCENARIOS = Path("tests/evidence/scenarios.json")
MAX_COMMENT_BYTES = 60_000  # GitHub's limit is 65,536 characters; CI refuses > 65,000 bytes
RUN_TIMEOUT_S = 60
VOLATILE = "<volatile>"

# UI section (F4): built here from ui_manifest.json, never pasted from a
# pre-rendered markdown file. The name pattern must stay identical to
# scripts/ui_evidence.py CAPTURE_RE (pinned by test), so only allow-listed
# capture files can become <img> tags.
SHA_PLACEHOLDER = "__HOUSESITTER_EVIDENCE_SHA__"  # must equal ui_evidence's
UI_STATUSES = ("added", "removed", "changed", "unchanged")
UI_CAPTURE_RE = re.compile(
    r"[a-z0-9](?:[a-z0-9-]|_(?!_)){0,47}"
    r"__(index-phone|index-desktop|index-desktop-dark|eink)"
    r"__(base|head|diff)\.png"
)
UI_VIEW_WIDTHS = {
    "index-phone": 240,
    "index-desktop": 480,
    "index-desktop-dark": 480,
    "eink": 400,
}
UI_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
UI_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_UI_TEXT_LEN = 200  # cap per displayed check failure (matches ui_evidence)
MAX_UI_LABEL_LEN = 100  # cap per displayed fixture/view label
# The comment intro names the browser that captured the screenshots. These
# values must stay identical to scripts/ui_evidence.py's (pinned by test):
# this module never imports ui_evidence, so it keeps its own copies.
UI_INTRO = (
    "Fixture house only. `scripts/ui_evidence.py` captures every fixture in "
    "`tests/fixtures/dashboard/` at phone (360x780), desktop (1280x900, light "
    "and dark), and e-ink (800x480) sizes."
)
UI_IMAGES_NOTE = (
    "Images live on the `pr-evidence` branch, pinned to the evidence push, "
    "so they never change afterwards."
)
UI_BROWSER_UNKNOWN = "The manifest does not record a recognised browser."
UI_BROWSER_NAMES = {
    "chromium": "Playwright's bundled Chromium",
    "chrome": "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "msedge": "Microsoft Edge",
}
UI_BROWSER_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){0,3}")
UI_BROWSER_VERSION_MAX_LEN = 32


class Refused(Exception):
    pass


def refuse_if_real_setup() -> None:
    if os.environ.get("HOUSESITTER_LIVE"):
        raise Refused("HOUSESITTER_LIVE is set; evidence uses the fixture house only")
    if os.environ.get("HOUSESITTER_CONFIG"):
        raise Refused("HOUSESITTER_CONFIG is set; evidence uses the fixture house only")
    real = Path.home() / ".config" / "housesitter" / "config.json"
    if real.exists():
        raise Refused(f"{real} exists; run evidence on a machine without a real setup")


def load_scenarios(tree: Path) -> tuple[list[dict], list[str]]:
    path = tree / SCENARIOS
    if not path.exists():
        return [], []
    data = json.loads(path.read_text())
    return data["scenarios"], data.get("volatile_keys", [])


def has_package(tree: Path) -> bool:
    return (tree / "src" / "housesitter" / "__init__.py").exists()


def run_scenario(tree: Path, scenario: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="hs-evidence-") as home:
        env = {
            "HOME": home,
            "USER": "evidence",
            "LOGNAME": "evidence",
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONPATH": str((tree / "src").resolve()),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        stdin = scenario.get("stdin")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "housesitter", *scenario["argv"]],
                cwd=home,
                env=env,
                input=(json.dumps(stdin) if stdin is not None else "").encode(),
                capture_output=True,
                timeout=RUN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "exit": "timeout",
                "stdout": decode(exc.stdout),
                "stderr": decode(exc.stderr),
                "sdk_size": None,
            }
    stdout = decode(proc.stdout)
    return {
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": decode(proc.stderr),
        # The Muse SDK's own output-size measure (executor.py _clip).
        "sdk_size": len(json.dumps(stdout)) - 2,
    }


def decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


def blank_volatile(obj: object, keys: set[str]) -> object:
    if isinstance(obj, dict):
        return {k: (VOLATILE if k in keys else blank_volatile(v, keys)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [blank_volatile(v, keys) for v in obj]
    return obj


def render(result: dict | None, volatile: set[str]) -> str:
    """Stable text form of one run, used for diffing and display."""
    if result is None:
        return "(not present)\n"
    out = result["stdout"]
    try:
        parsed = json.loads(out)
    except ValueError:
        pass
    else:
        out = json.dumps(blank_volatile(parsed, volatile), indent=2, sort_keys=True) + "\n"
    text = f"$ exit {result['exit']}\n--- stdout ---\n{out}"
    if result["stderr"]:
        text += f"--- stderr ---\n{result['stderr']}"
    if not text.endswith("\n"):
        text += "\n"
    return text


def code_span(text: str) -> str:
    ticks = "`"
    while ticks in text:
        ticks += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def fence(text: str, lang: str = "") -> str:
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}{lang}\n{text}{ticks}\n"


def _ui_cell(text: object, limit: int) -> str:
    """Make manifest text safe for a Markdown table cell.

    Collapses whitespace (no newlines to break rows), swaps `|` for `/` (so a
    hostile value cannot add columns), and caps the length. Callers wrap the
    result in a code span, which neutralises every other Markdown/HTML-active
    character: the text stays readable but nothing in it renders.
    """
    return " ".join(str(text).split()).replace("|", "/")[:limit]


def _ui_img(src: str, width: int, alt: str) -> str:
    """An <img> for an evidence-branch file. Only allow-listed names render."""
    name = src.rsplit("/", 1)[-1]
    if not UI_CAPTURE_RE.fullmatch(name):
        raise ValueError(f"refusing <img> for unexpected file name: {name!r}")
    return (
        f'<img src="{html.escape(src, quote=True)}" '
        f'width="{width:d}" loading="lazy" alt="{html.escape(alt, quote=True)}">'
    )


def browser_display(manifest: dict) -> str | None:
    """E.g. "Google Chrome 131.0.6778" for a recognised browser, else None.

    The manifest is untrusted data (a PR-authored artifact), so the channel
    goes through an allow-list and the version through a strict numeric
    shape; anything else renders the neutral sentence, never the bad value.
    Mirrors ui_evidence.browser_display (pinned by test).
    """
    browser = manifest.get("browser")
    if not isinstance(browser, dict):
        return None
    channel = browser.get("channel")
    version = browser.get("version")
    if not isinstance(channel, str) or not isinstance(version, str):
        return None
    name = UI_BROWSER_NAMES.get(channel)
    if name is None:
        return None
    if len(version) > UI_BROWSER_VERSION_MAX_LEN:
        return None
    if not UI_BROWSER_VERSION_RE.fullmatch(version):
        return None
    return f"{name} {version}"


def load_ui_manifest(ui_dir: Path | None) -> dict | None:
    """Read the UI manifest, or None when there is no UI evidence.

    A corrupt (unparseable) manifest raises: the capture pipeline is broken
    and failing closed beats posting a comment that silently omits the UI.
    """
    if ui_dir is None:
        return None
    path = Path(ui_dir) / "ui_manifest.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("captures"), list):
        raise ValueError("UI manifest must be an object with a captures list")
    return data


# Names a status must not carry: the comment never references them, so the
# push must not carry them either. Added shows only head, removed only base,
# unchanged shows no image at all (its PNGs are deleted by build_manifest).
UI_FORBIDDEN_NAMES = {
    "added": ("base", "diff"),
    "removed": ("head", "diff"),
    "unchanged": ("base", "head", "diff"),
}


def select_ui_captures(manifest: dict) -> tuple:
    """Split the manifest into kept captures and a dropped count.

    Returns ([(capture, referenced files)], dropped): for every kept
    capture, exactly the PNG names the details block references as <img>
    URLs (added -> head; removed -> base; changed -> base, head, and diff
    when set; unchanged -> none). This is the single decision point for
    which images the full Web UI section pictures;
    publish_evidence.copy_pushable_files copies exactly these names (plus
    the manifest), so the push carries exactly what the full section
    pictures. When the comment budget forces the table-only fallback, the
    pushed images are not pictured (docs/REVIEW.md, "Retention").

    Besides the needed names per status, added/removed/unchanged forbid
    the names above; a non-None diff_error is allowed only on a changed
    capture without a diff image; and removed forbids non-empty
    head_problems (a removed page has no head run to report checks for).
    A violating entry is dropped, like every other invalid entry.
    """
    kept, dropped = [], 0
    for cap in manifest.get("captures", []):
        if not isinstance(cap, dict):
            dropped += 1
            continue
        status = cap.get("status")
        names = {key: cap.get(key) for key in ("base", "head", "diff")}
        problems = cap.get("head_problems", [])
        view = cap.get("view")
        if status not in UI_STATUSES or view not in UI_VIEW_WIDTHS:
            dropped += 1
            continue
        if any(
            value is not None
            and (not isinstance(value, str) or not UI_CAPTURE_RE.fullmatch(value))
            for value in names.values()
        ):
            dropped += 1
            continue
        if not isinstance(problems, list) or any(
            not isinstance(item, str) for item in problems
        ):
            dropped += 1
            continue
        diff_error = cap.get("diff_error")
        if diff_error is not None and not isinstance(diff_error, str):
            dropped += 1
            continue
        need = {"added": ("head",), "removed": ("base",), "changed": ("base", "head")}.get(
            status, ()
        )
        if any(not isinstance(names[key], str) for key in need):
            dropped += 1
            continue
        forbidden = UI_FORBIDDEN_NAMES.get(status, ())
        if any(names[key] is not None for key in forbidden):
            dropped += 1
            continue
        # A diff error only exists for a changed pair whose diff failed, so
        # it never comes with a diff image or with any other status.
        if diff_error is not None and (status != "changed" or names["diff"] is not None):
            dropped += 1
            continue
        if status == "removed" and len(problems) != 0:
            dropped += 1
            continue
        if status == "added":
            files = [names["head"]]
        elif status == "removed":
            files = [names["base"]]
        elif status == "changed":
            files = [names["base"], names["head"]]
            if names["diff"] is not None:
                files.append(names["diff"])
        else:
            files = []
        kept.append((cap, files))
    return kept, dropped


def render_ui_section(manifest: dict, repo: str, pr: int, head_sha: str) -> tuple:
    """Build (full section, table-only section, check-failure count).

    Every manifest value is re-validated: capture names against the capture
    regex, status against the enum, the view against the known views, and
    problems escaped (via code spans) and length-capped. Captures that fail
    validation are dropped and counted, never rendered. Image URLs are built
    from the validated repo/PR/head SHA plus the validated file name, so they
    always match the pinned evidence-branch pattern exactly. The browser key
    is untrusted too: only an allow-listed channel with a strict numeric
    version is named, otherwise a neutral sentence renders instead.
    """
    if not UI_REPO_RE.fullmatch(repo or ""):
        raise ValueError(f"bad repo: {repo!r}")
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError(f"bad PR number: {pr!r}")
    if not UI_SHA_RE.fullmatch(head_sha or ""):
        raise ValueError(f"bad head SHA: {head_sha!r}")
    url = (
        f"https://raw.githubusercontent.com/{repo}/{SHA_PLACEHOLDER}"
        f"/pr/{pr}/{head_sha}"
    )
    kept_with_files, dropped = select_ui_captures(manifest)
    kept = [cap for cap, _ in kept_with_files]
    lines = [
        "## Web UI",
        "",
        UI_INTRO,
        "",
    ]
    if kept:
        display = browser_display(manifest)
        if display is None:
            lines += [UI_BROWSER_UNKNOWN + " " + UI_IMAGES_NOTE, ""]
        else:
            lines += ["Captured headless with " + display + ". " + UI_IMAGES_NOTE, ""]
    if not manifest["captures"]:
        lines += [
            "Neither the base nor the head captured anything: no `web/` on "
            "either side, so there is nothing to show.",
            "",
        ]
    failures = 0
    if kept:
        lines += [
            "| Capture | Status | Checks |",
            "|---------|--------|--------|",
        ]
        for cap in kept:
            # Validation treats a missing head_problems as none, so must this.
            checks = [
                _ui_cell(item, MAX_UI_TEXT_LEN) for item in cap.get("head_problems", [])
            ]
            if cap.get("diff_error"):
                checks.append(_ui_cell(cap["diff_error"], MAX_UI_TEXT_LEN))
            if checks:
                failures += 1
            label = _ui_cell(
                f"{cap.get('fixture', '')} / {cap.get('view', '')}", MAX_UI_LABEL_LEN
            )
            checks_cell = code_span("; ".join(checks) if checks else "pass")
            lines.append(f"| {code_span(label)} | {cap['status']} | {checks_cell} |")
    lines += [""]
    if dropped:
        lines += [
            f"**Omitted {dropped} UI capture(s) with invalid manifest entries.**",
            "",
        ]
    table_section = "\n".join(lines)

    details = []
    by_fixture: dict = {}
    for cap in kept:
        by_fixture.setdefault(_ui_cell(cap.get("fixture", ""), MAX_UI_LABEL_LEN), []).append(cap)
    for fixture in sorted(by_fixture):
        shown = [c for c in by_fixture[fixture] if c["status"] in ("added", "changed", "removed")]
        if not shown:
            continue
        block = [f"<details><summary>{html.escape(fixture)}</summary>", ""]
        for cap in shown:
            label = _ui_cell(f"{cap.get('fixture', '')} / {cap.get('view', '')}", MAX_UI_LABEL_LEN)
            width = UI_VIEW_WIDTHS[cap["view"]]
            block += [f"#### {code_span(label)}", ""]
            if cap["status"] == "added":
                block += [
                    "New page: no before image (the base doesn't have this page).",
                    "",
                    _ui_img(f"{url}/{cap['head']}", width, f"after: {label}"),
                    "",
                ]
            elif cap["status"] == "removed":
                block += [
                    "Removed page: no after image (the head no longer has this page).",
                    "",
                    _ui_img(f"{url}/{cap['base']}", width, f"before: {label}"),
                    "",
                ]
            else:
                block += [
                    _ui_img(f"{url}/{cap['base']}", width, f"before: {label}"),
                    _ui_img(f"{url}/{cap['head']}", width, f"after: {label}"),
                ]
                if cap.get("diff") is not None:
                    block.append(_ui_img(f"{url}/{cap['diff']}", width, f"diff: {label}"))
                else:
                    reason = _ui_cell(
                        cap.get("diff_error") or "unknown reason", MAX_UI_TEXT_LEN
                    )
                    # A code span, like the table: no markdown or HTML renders.
                    block.append("Diff unavailable: " + code_span(reason) + ".")
                block += [""]
        block += ["</details>", ""]
        details.append((fixture, "\n".join(block)))

    # No budget trimming here: the caller picks whole tiers (full section,
    # table alone, or a one-line pointer) against the shared comment budget,
    # so a cut never leaves a tag or fence open.
    full = table_section
    for _, block in details:
        full += ("\n" if not full.endswith("\n\n") else "") + block
    return full, table_section, failures


def append_ui_section(
    body: str,
    ui_dir: Path | None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    """Append the Web UI section, built from ui_manifest.json, if present.

    Pre-rendered markdown files in the artifact are ignored: the comment
    carries only known boilerplate plus a table and images built from the
    re-validated manifest. Without repo/PR/head-SHA context the pinned image
    URLs cannot be built, so the body is returned unchanged.
    """
    manifest = load_ui_manifest(ui_dir)
    if manifest is None or repo is None or pr is None or head_sha is None:
        return body
    room = MAX_COMMENT_BYTES - len(body.encode()) - 400
    full, table, _ = render_ui_section(manifest, repo, pr, head_sha)
    if len(("\n" + full).encode()) <= room:
        return body + "\n" + full
    fallback = (
        "\n" + table + "\n**Web UI details omitted to fit a GitHub comment:** "
        "run `scripts/ui_evidence.py` locally to see them.\n"
    )
    if len(fallback.encode()) <= room:
        return body + fallback
    return (
        body + "\n**Web UI evidence omitted to fit a GitHub comment:** "
        "run `scripts/ui_evidence.py` locally to see it.\n"
    )


def build_comment(
    base_tree: Path,
    head_tree: Path,
    ui_dir: Path | None = None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    scenarios, volatile_keys = load_scenarios(head_tree)
    volatile = set(volatile_keys)
    base_scenarios = load_scenarios(base_tree)[0]
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    base_ok = has_package(base_tree)
    ui_manifest = load_ui_manifest(ui_dir)
    ui_failures = 0
    if ui_manifest is not None and repo is not None:
        _, _, ui_failures = render_ui_section(ui_manifest, repo, pr, head_sha)

    def size(r: dict | None) -> str:
        return "—" if r is None or r["sdk_size"] is None else f"{r['sdk_size']:,} B"

    def exit_(r: dict | None) -> str:
        return "—" if r is None else str(r["exit"])

    rows, sections, unexpected = [], [], []
    for scenario in scenarios:
        before = run_scenario(base_tree, scenario) if base_ok else None
        after = run_scenario(head_tree, scenario)
        before_text, after_text = render(before, volatile), render(after, volatile)
        if before is None or scenario["id"] not in base_ids:
            status = "new"
        elif before_text == after_text:
            status = "unchanged"
        else:
            status = "changed"
        expected = scenario.get("expect_exit", 0)
        if after["exit"] != expected:
            unexpected.append(
                f"{code_span(scenario['id'])} exited {after['exit']}, expected {expected}"
            )
        rows.append(
            f"| {code_span(scenario['id'])} | {status} | {exit_(before)} → {exit_(after)} "
            f"| {size(before)} → {size(after)} |"
        )
        if status == "unchanged":
            continue
        diff = "".join(
            difflib.unified_diff(
                before_text.splitlines(keepends=True),
                after_text.splitlines(keepends=True),
                fromfile="before",
                tofile="after",
            )
        )
        # Scenario ids and titles come from the head tree's scenarios file, so
        # they are PR-authored data: both go through code_span (which grows
        # the backticks when the value contains any) so neither can inject
        # Markdown or HTML into the heading.
        heading = f"### {code_span(scenario['id'])}"
        if scenario.get("title", ""):
            heading += f": {code_span(scenario['title'])}"
        sections.append(
            heading + "\n\n"
            + code_span("housesitter " + " ".join(scenario["argv"]))
            + "\n\n"
            + fence(diff, "diff")
            + "\n<details><summary>Full before</summary>\n\n"
            + fence(before_text)
            + "\n</details>\n<details><summary>Full after</summary>\n\n"
            + fence(after_text)
            + "\n</details>\n"
        )
    for scenario in base_scenarios:
        if scenario["id"] not in head_ids:
            rows.append(f"| {code_span(scenario['id'])} | removed | — | — |")

    head = [
        MARKER,
        "## Before / after evidence",
        "",
        "Fixture house only. Generated by `scripts/pr_evidence.py` from "
        "`tests/evidence/scenarios.json`; see docs/REVIEW.md, \"Evidence\". "
        "Size is the Muse SDK's measure of stdout (limit 96 KiB; budget 88 KiB).",
        "",
    ]
    if ui_failures:
        head += [
            f"**Web UI checks failed:** {ui_failures} capture(s) report "
            "problems; see the Web UI section below.",
            "",
        ]
    if not base_ok:
        head += ["The base has no `housesitter` package yet, so every scenario is new.", ""]
    if not scenarios:
        head += ["**The head has no scenarios.** Every PR must keep at least one.", ""]
    for line in unexpected:
        head += [f"**Unexpected exit:** {line}.", ""]
    head += [
        "| Scenario | Status | Exit before → after | Size before → after |",
        "|----------|--------|---------------------|---------------------|",
        *rows,
        "",
    ]
    if not sections and not any("| removed |" in r for r in rows):
        head += ["**No output change.**", ""]
    top = "\n".join(head) + "\n"

    # Keep whole sections only, so a cut never leaves a code fence open.
    kept, omitted = [], []
    budget = MAX_COMMENT_BYTES - len(top.encode()) - 400
    for section in sections:
        cost = len(section.encode()) + 1
        if cost <= budget:
            kept.append(section)
            budget -= cost
        else:
            omitted.append(section.split("\n", 1)[0].removeprefix("### "))
    body = top + "\n".join(kept)
    if omitted:
        body += (
            "\n**Omitted to fit a GitHub comment:** "
            + ", ".join(omitted)
            + ". Run `scripts/pr_evidence.py` locally to see them.\n"
        )
    return append_ui_section(body, ui_dir, repo=repo, pr=pr, head_sha=head_sha)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-tree", type=Path, required=True)
    parser.add_argument("--head-tree", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--ui-artifacts",
        type=Path,
        default=None,
        help="directory with ui_manifest.json from scripts/ui_evidence.py",
    )
    parser.add_argument(
        "--repo", default=None, help="owner/name of the GitHub repo (with --ui-artifacts)"
    )
    parser.add_argument(
        "--pr", type=int, default=None, help="pull request number (with --ui-artifacts)"
    )
    parser.add_argument(
        "--head-sha", default=None, help="head commit SHA, 40 hex (with --ui-artifacts)"
    )
    args = parser.parse_args(argv)
    if args.ui_artifacts is not None and not (args.repo and args.pr and args.head_sha):
        parser.error("--repo, --pr, and --head-sha are required with --ui-artifacts")
    try:
        refuse_if_real_setup()
    except Refused as exc:
        print(f"pr_evidence: refused: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(
        build_comment(
            args.base_tree,
            args.head_tree,
            args.ui_artifacts,
            repo=args.repo,
            pr=args.pr,
            head_sha=args.head_sha,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
