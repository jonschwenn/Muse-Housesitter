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
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "<!-- housesitter-evidence -->"
SCENARIOS = Path("tests/evidence/scenarios.json")
MAX_COMMENT_BYTES = 60_000  # GitHub's limit is 65,536 characters; CI refuses > 65,000 bytes
RUN_TIMEOUT_S = 60
VOLATILE = "<volatile>"


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


def build_comment(base_tree: Path, head_tree: Path) -> str:
    scenarios, volatile_keys = load_scenarios(head_tree)
    volatile = set(volatile_keys)
    base_scenarios = load_scenarios(base_tree)[0]
    base_ids = {s["id"] for s in base_scenarios}
    head_ids = {s["id"] for s in scenarios}
    base_ok = has_package(base_tree)

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
            unexpected.append(f"`{scenario['id']}` exited {after['exit']}, expected {expected}")
        rows.append(
            f"| `{scenario['id']}` | {status} | {exit_(before)} → {exit_(after)} "
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
        sections.append(
            f"### `{scenario['id']}`: {scenario.get('title', '')}\n\n"
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
            rows.append(f"| `{scenario['id']}` | removed | — | — |")

    head = [
        MARKER,
        "## Before / after evidence",
        "",
        "Fixture house only. Generated by `scripts/pr_evidence.py` from "
        "`tests/evidence/scenarios.json`; see docs/REVIEW.md, \"Evidence\". "
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-tree", type=Path, required=True)
    parser.add_argument("--head-tree", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        refuse_if_real_setup()
    except Refused as exc:
        print(f"pr_evidence: refused: {exc}", file=sys.stderr)
        return 2
    args.out.write_text(build_comment(args.base_tree, args.head_tree), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
