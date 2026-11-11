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
# Valid ``crop_px`` heights a manifest may record (Decision 5): ui_evidence
# crops full-page shots down these steps when the push cap would refuse
# them. Must equal ui_evidence.CROP_STEPS[1:] (pinned by test); this module
# never imports ui_evidence, so it keeps its own copy.
UI_CROP_HEIGHTS = (6000, 4000, 3000)


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
        crop_px = cap.get("crop_px")
        if crop_px is not None and (
            type(crop_px) is not int or crop_px not in UI_CROP_HEIGHTS
        ):
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
            if cap.get("crop_px") is not None:
                # select_ui_captures above kept only valid heights, so this
                # int is safe to format: fixed words plus the recorded
                # height. A cropped shot shows only the top of the page.
                block.append(
                    f"Cropped at {cap['crop_px']:,} px to fit the size limit; "
                    "changes below this line are not shown."
                )
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


def collect_runs(base_tree: Path, head_tree: Path) -> tuple:
    """Run every head scenario against both trees. Returns
    (scenarios, volatile, base_scenarios, base_ok, runs) where runs is a list
    of (scenario, before, after); before is None when the base has no
    package. Both comment builders below share this, so the text comment
    and the structured results describe the same runs."""
    scenarios, volatile_keys = load_scenarios(head_tree)
    volatile = set(volatile_keys)
    base_scenarios = load_scenarios(base_tree)[0]
    base_ok = has_package(base_tree)
    runs = []
    for scenario in scenarios:
        before = run_scenario(base_tree, scenario) if base_ok else None
        after = run_scenario(head_tree, scenario)
        runs.append((scenario, before, after))
    return scenarios, volatile, base_scenarios, base_ok, runs


def build_results(
    base_tree: Path, head_tree: Path, head_sha: str | None = None
) -> dict:
    """Run every scenario and return the structured results (Decision 13,
    option A). The privileged publisher validates this against a strict
    schema and renders the comment itself; the PR's markdown is never
    posted. ``before``/``after`` hold the normalised texts ``render``
    produces (the same strings the comment diffs); ``before`` is None when
    the base has no package to run against."""
    scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(base_tree, head_tree)
    return _results_from_runs(scenarios, volatile, base_scenarios, base_ok, runs, head_sha)


def _results_from_runs(
    scenarios: list,
    volatile: set,
    base_scenarios: list,
    base_ok: bool,
    runs: list,
    head_sha: str | None,
) -> dict:
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    entries = []
    for scenario, before, after in runs:
        before_text = render(before, volatile) if before is not None else None
        after_text = render(after, volatile)
        new = before is None or scenario["id"] not in base_ids
        entries.append(
            {
                "id": scenario["id"],
                "title": scenario.get("title", ""),
                "command": ["housesitter", *scenario["argv"]],
                "exit_before": before["exit"] if before is not None else None,
                "exit_after": after["exit"],
                "expected_exit": scenario.get("expect_exit", 0),
                "size_before": before["sdk_size"] if before is not None else None,
                "size_after": after["sdk_size"],
                "changed": new or before_text != after_text,
                "new_in_pr": scenario["id"] not in base_ids,
                "before": before_text,
                "after": after_text,
            }
        )
    metadata: dict = {"generator": "scripts/pr_evidence.py", "base_has_package": base_ok}
    if head_sha is not None:
        metadata["head_sha"] = head_sha
    return {
        "version": 1,
        "metadata": metadata,
        "scenarios": entries,
        "removed": sorted(base_ids - head_ids),
    }


def neutralise(text: object) -> str:
    """Break up @mentions and <tags> in PR-supplied text.

    Fenced code blocks already stop GitHub rendering mentions, links, and
    HTML, but this breaks them up anyway (an invisible separator after each
    ``@`` and ``<``), so the raw bytes cannot ping or tag even if a fence
    is ever dropped. Idempotent and invisible in the rendered comment.
    """
    return str(text).replace("@", "@\u200b").replace("<", "<\u200b")


def _inline(text: object) -> str:
    """Single-line form of PR-supplied text for headings and table cells."""
    return neutralise(" ".join(str(text).split()))


def render_text_from_results(results: dict, head_sha: str) -> str:
    """Rebuild the text section from validated structured results (main's
    code: the privileged publisher calls this, never PR code).

    The caller must have validated ``results`` (unknown keys rejected,
    types and caps checked, ids known or marked new). Every PR-supplied
    string appears only inside fenced code blocks (whose fence outgrows any
    backtick run in the content) or single-line code spans, neutralised, so
    a forged "Approved by maintainer" can never render as a heading,
    prose, link, or image. The fixed header says the outputs came from the
    PR's own CI run: they are author-attested, not trusted fact.
    """
    scenarios = results["scenarios"]
    removed = results.get("removed", [])
    base_ok = results.get("metadata", {}).get("base_has_package", True)

    def size(value: object) -> str:
        return "—" if value is None else f"{value:,} B"

    def exit_(value: object) -> str:
        return "—" if value is None else str(value)

    rows, sections, unexpected = [], [], []
    for entry in scenarios:
        before_text = entry["before"] if entry["before"] is not None else "(not present)\n"
        after_text = entry["after"]
        before_text, after_text = neutralise(before_text), neutralise(after_text)
        if entry.get("new_in_pr"):
            status = "new"
        elif not entry["changed"]:
            status = "unchanged"
        else:
            status = "changed"
        if entry["exit_after"] != entry["expected_exit"]:
            unexpected.append(
                f"{code_span(_inline(entry['id']))} exited "
                f"{entry['exit_after']}, expected {entry['expected_exit']}"
            )
        rows.append(
            f"| {code_span(_inline(entry['id']))} | {status} "
            f"| {exit_(entry['exit_before'])} → {exit_(entry['exit_after'])} "
            f"| {size(entry['size_before'])} → {size(entry['size_after'])} |"
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
        heading = f"### {code_span(_inline(entry['id']))}"
        if entry.get("title", ""):
            heading += f": {code_span(_inline(entry['title']))}"
        command = " ".join(_inline(arg) for arg in entry["command"])
        sections.append(
            heading + "\n\n"
            + code_span(command)
            + "\n\n"
            + fence(diff, "diff")
            + "\n<details><summary>Full before</summary>\n\n"
            + fence(before_text)
            + "\n</details>\n<details><summary>Full after</summary>\n\n"
            + fence(after_text)
            + "\n</details>\n"
        )
    for ident in removed:
        rows.append(f"| {code_span(_inline(ident))} | removed | — | — |")

    head = [
        MARKER,
        "## Before / after evidence",
        "",
        "Fixture house only. Rebuilt by main's publisher from the PR's structured "
        "results (`results.json`); see docs/REVIEW.md, \"Evidence\". The outputs below "
        f"were produced by the PR's own CI run at `{head_sha}` from the PR's own code: "
        "they are author-attested, like the screenshots, not trusted fact. "
        "Size is the Muse SDK's measure of stdout (limit 96 KiB; budget 88 KiB).",
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
    return body


def build_comment(
    base_tree: Path,
    head_tree: Path,
    ui_dir: Path | None = None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(base_tree, head_tree)
    return _comment_from_runs(
        scenarios, volatile, base_scenarios, base_ok, runs, ui_dir,
        repo=repo, pr=pr, head_sha=head_sha,
    )


def _comment_from_runs(
    scenarios: list,
    volatile: set,
    base_scenarios: list,
    base_ok: bool,
    runs: list,
    ui_dir: Path | None,
    *,
    repo: str | None = None,
    pr: int | None = None,
    head_sha: str | None = None,
) -> str:
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    ui_manifest = load_ui_manifest(ui_dir)
    ui_failures = 0
    if ui_manifest is not None and repo is not None:
        _, _, ui_failures = render_ui_section(ui_manifest, repo, pr, head_sha)

    def size(r: dict | None) -> str:
        return "—" if r is None or r["sdk_size"] is None else f"{r['sdk_size']:,} B"

    def exit_(r: dict | None) -> str:
        return "—" if r is None else str(r["exit"])

    rows, sections, unexpected = [], [], []
    for scenario, before, after in runs:
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
    parser.add_argument(
        "--results-out",
        type=Path,
        default=None,
        help="write the structured scenario results here as JSON "
        "(Decision 13, option A: the publisher renders from this, never from --out)",
    )
    args = parser.parse_args(argv)
    if args.ui_artifacts is not None and not (args.repo and args.pr and args.head_sha):
        parser.error("--repo, --pr, and --head-sha are required with --ui-artifacts")
    try:
        refuse_if_real_setup()
    except Refused as exc:
        print(f"pr_evidence: refused: {exc}", file=sys.stderr)
        return 2
    if args.results_out is not None:
        # One shared collection run feeds both outputs, so the text comment
        # (for the old publisher, while this rolls out) and the structured
        # results (for the new one) describe the same runs.
        scenarios, volatile, base_scenarios, base_ok, runs = collect_runs(
            args.base_tree, args.head_tree
        )
        args.out.write_text(
            _comment_from_runs(
                scenarios,
                volatile,
                base_scenarios,
                base_ok,
                runs,
                args.ui_artifacts,
                repo=args.repo,
                pr=args.pr,
                head_sha=args.head_sha,
            ),
            encoding="utf-8",
        )
        args.results_out.write_text(
            json.dumps(
                _results_from_runs(
                    scenarios, volatile, base_scenarios, base_ok, runs, args.head_sha
                ),
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
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
