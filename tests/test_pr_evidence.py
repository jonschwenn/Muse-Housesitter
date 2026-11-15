"""Tests for scripts/pr_evidence.py, run against throwaway source trees."""

from __future__ import annotations

import importlib.util
import json
import random
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from core.helpers import require_test_tool

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("pr_evidence", ROOT / "scripts" / "pr_evidence.py")
pr_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_evidence)
CHECKS_SPEC = importlib.util.spec_from_file_location(
    "evidence_checks", ROOT / "scripts" / "evidence_checks.py"
)
evidence_checks = importlib.util.module_from_spec(CHECKS_SPEC)
CHECKS_SPEC.loader.exec_module(evidence_checks)

openssl = shutil.which("openssl")


def make_tree(tmp_path: Path, name: str, version: str | None, scenarios: list[dict]) -> Path:
    tree = tmp_path / name
    (tree / "tests" / "evidence").mkdir(parents=True)
    (tree / "tests" / "evidence" / "scenarios.json").write_text(
        json.dumps({"volatile_keys": ["collected_at"], "scenarios": scenarios})
    )
    if version is not None:
        shutil.copytree(ROOT / "src", tree / "src", ignore=shutil.ignore_patterns("__pycache__"))
        init = tree / "src" / "housesitter" / "__init__.py"
        init.write_text(init.read_text().replace(pr_evidence_version(), version))
    return tree


def pr_evidence_version() -> str:
    from housesitter import __version__

    return __version__


VERSION = [{"id": "version", "title": "Installed version", "argv": ["--version"]}]


def test_base_without_package_marks_everything_new(tmp_path):
    base = make_tree(tmp_path, "base", None, [])
    head = make_tree(tmp_path, "head", "9.9.9", VERSION)
    body = pr_evidence.build_comment(base, head)
    assert body.startswith(pr_evidence.MARKER)
    assert "| `version` | new |" in body
    assert "+housesitter 9.9.9" in body


def test_identical_trees_report_no_output_change(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.0.0", VERSION)
    body = pr_evidence.build_comment(base, head)
    assert "| `version` | unchanged |" in body
    assert "**No output change.**" in body
    assert "```diff" not in body


def test_changed_output_shows_a_diff(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.1.0", VERSION)
    body = pr_evidence.build_comment(base, head)
    assert "| `version` | changed |" in body
    assert "-housesitter 1.0.0" in body
    assert "+housesitter 1.1.0" in body


def test_volatile_keys_are_blanked():
    rendered = pr_evidence.render(
        {"exit": 0, "stdout": '{"collected_at": "2026-11-06T00:00:00Z", "ok": true}', "stderr": ""},
        {"collected_at"},
    )
    assert "2026-11-06" not in rendered
    assert pr_evidence.VOLATILE in rendered


def test_runs_with_an_empty_home_and_minimal_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOMETHING_SECRET", "PLANTED-SECRET-env")
    tree = make_tree(tmp_path, "t", "1.0.0", [])
    probe = tree / "src" / "housesitter" / "__main__.py"
    probe.write_text(
        "import json, os, pathlib\n"
        "print(json.dumps({'env': sorted(os.environ),"
        " 'home_empty': not any(pathlib.Path(os.environ['HOME']).iterdir())}))\n"
    )
    result = pr_evidence.run_scenario(tree, {"id": "probe", "argv": []})
    seen = json.loads(result["stdout"])
    assert seen["home_empty"] is True
    assert "SOMETHING_SECRET" not in seen["env"]
    allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    assert set(seen["env"]) <= allowed | {"LC_CTYPE"}  # Python may set LC_CTYPE itself


@pytest.mark.parametrize("var", ["HOUSESITTER_LIVE", "HOUSESITTER_CONFIG"])
def test_refuses_when_pointed_at_a_real_setup(monkeypatch, var):
    monkeypatch.setenv(var, "1")
    with pytest.raises(pr_evidence.Refused):
        pr_evidence.refuse_if_real_setup()


def test_refuses_when_a_real_config_exists(tmp_path, monkeypatch):
    monkeypatch.delenv("HOUSESITTER_LIVE", raising=False)
    monkeypatch.delenv("HOUSESITTER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "housesitter").mkdir(parents=True)
    (tmp_path / ".config" / "housesitter" / "config.json").write_text("{}")
    with pytest.raises(pr_evidence.Refused):
        pr_evidence.refuse_if_real_setup()


def test_every_scenario_in_the_repo_exits_as_expected():
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    assert scenarios, "tests/evidence/scenarios.json must list at least one scenario"
    for scenario in scenarios:
        result = pr_evidence.run_scenario(ROOT, scenario, volatile=set(volatile_keys))
        if result.get("skipped"):
            # No fake available (openssl missing and not required): the
            # skip is reported, not silent.
            require_test_tool(False, result["skipped"])
            continue
        assert result["exit"] == scenario.get("expect_exit", 0), (scenario["id"], result["stderr"])


def test_removed_scenarios_are_listed(tmp_path):
    help_ = [{"id": "help", "argv": ["--help"]}]
    base = make_tree(tmp_path, "base", "1.0.0", VERSION + help_)
    head = make_tree(tmp_path, "head", "1.0.0", VERSION)
    body = pr_evidence.build_comment(base, head)
    assert "| `help` | removed |" in body
    assert "No output change" not in body


def test_unexpected_exit_is_called_out(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", [])
    head = make_tree(tmp_path, "head", "1.0.0", [{"id": "bad", "argv": ["--nope"]}])
    body = pr_evidence.build_comment(base, head)
    assert "**Unexpected exit:** `bad` exited 2, expected 0." in body


def test_oversized_comment_drops_whole_sections(tmp_path, monkeypatch):
    many = [{"id": f"s{i}", "argv": ["--help"]} for i in range(30)]
    base = make_tree(tmp_path, "base", None, [])
    head = make_tree(tmp_path, "head", "1.0.0", many)
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 6000)
    body = pr_evidence.build_comment(base, head)
    assert len(body.encode()) <= 6000
    assert "**Omitted to fit a GitHub comment:**" in body
    assert body.count("<details>") == body.count("</details>")
    # Every opened fence is closed: fences come in pairs.
    assert sum(1 for line in body.splitlines() if line.startswith("```")) % 2 == 0


def test_non_utf8_output_does_not_crash(tmp_path):
    tree = make_tree(tmp_path, "t", "1.0.0", [])
    (tree / "src" / "housesitter" / "__main__.py").write_text(
        "import sys\nsys.stdout.buffer.write(b'caf\\xe9\\n')\n"
    )
    result = pr_evidence.run_scenario(tree, {"id": "x", "argv": []})
    assert result["exit"] == 0
    assert result["stdout"] == "caf\ufffd\n"


def test_code_span_survives_backticks():
    assert pr_evidence.code_span("a`b") == "``a`b``"
    assert pr_evidence.code_span("`x") == "`` `x ``"


def _fence_search_loop(text, lang=""):
    """The old quadratic fence choice: the oracle for the linear pass."""
    ticks = "```"
    while ticks in text:
        ticks += "`"
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    return f"{ticks}{lang}\n{text}{ticks}\n"


def test_fence_matches_search_loop_on_adversarial_inputs():
    # Random backtick/tick-adjacent runs: the linear pass must pick exactly
    # the fence the old search loop picked.
    import random

    rng = random.Random(20261111)
    cases = ["", "`", "``", "```", "a`b``c```d", "`" * 100, "x" * 1000]
    for _ in range(200):
        alphabet = ["`", "`", "a", "\n", "~"]
        cases.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60))))
    for text in cases:
        assert pr_evidence.fence(text) == _fence_search_loop(text)
        assert pr_evidence.fence(text, "diff") == _fence_search_loop(text, "diff")


def test_fence_handles_million_tick_runs_quickly():
    # Review round 2, finding 4: schema-valid output of a million backticks
    # made fence selection scan ever-longer strings (100k took ~4 s, the
    # full artifact ran past 30 s). One linear pass must stay far under 2 s.
    import time

    text = "`" * 1_000_000
    start = time.monotonic()
    out = pr_evidence.fence(text)
    assert time.monotonic() - start < 2
    assert out.startswith("`" * 1_000_001 + "\n")
    assert out.endswith("`" * 1_000_001 + "\n")


def test_render_text_handles_million_tick_runs_quickly():
    # End to end: the comment-budget trimming path over hostile output with
    # million-tick runs must also finish quickly (trimming runs over text
    # the linear fence already shaped, with no other super-linear work).
    import time

    ticks = "`" * 1_000_000
    results = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "base_has_package": True},
        "scenarios": [
            {
                "id": "tickbomb",
                "title": "tick bomb",
                "command": ["housesitter", "--version"],
                "exit_before": 0,
                "exit_after": 0,
                "expected_exit": 0,
                "size_before": 10,
                "size_after": 10,
                "changed": True,
                "new_in_pr": False,
                "before": "$ exit 0\n" + ticks + "\n",
                "after": "$ exit 0\n" + ticks + "\n",
            }
        ],
        "removed": [],
    }
    start = time.monotonic()
    body = pr_evidence.render_text_from_results(results, "a" * 40)
    assert time.monotonic() - start < 2
    assert body.startswith(pr_evidence.MARKER)


HOSTILE_TITLE = '<img src="https://evil.example/x.png"> **All UI checks passed** <!-- x -->'


def test_hostile_scenario_title_cannot_inject_markup(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", [])
    head = make_tree(
        tmp_path, "head", "1.0.0", [{"id": "ok", "title": HOSTILE_TITLE, "argv": ["--version"]}]
    )
    body = pr_evidence.build_comment(base, head)
    # The title must be wrapped in a code span, never pasted as raw Markdown/HTML.
    assert f"`{HOSTILE_TITLE}`" in body
    assert f"### `ok`: {HOSTILE_TITLE}\n" not in body


def _ui_manifest(*captures):
    return {"version": 1, "captures": list(captures)}


def _ui_capture(name="typical__index-phone__head.png", **overrides):
    capture = {
        "fixture": "typical",
        "view": "index-phone",
        "status": "added",
        "base": None,
        "head": name,
        "diff": None,
        "head_problems": [],
    }
    capture.update(overrides)
    return capture


UI_REPO = "example-owner/example-repo"
UI_PR = 7
UI_SHA = "b" * 40


def _ui_dir_with_manifest(tmp_path, manifest):
    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "ui_manifest.json").write_text(json.dumps(manifest))
    return ui_dir


def test_ui_section_is_built_from_the_manifest_not_from_markdown(tmp_path):
    manifest = _ui_manifest(_ui_capture())
    ui_dir = _ui_dir_with_manifest(tmp_path, manifest)
    # A pre-rendered markdown file must be ignored, even if hostile.
    (ui_dir / "ui_section.md").write_text("PWNED <script>alert(1)</script>\n")
    (ui_dir / "ui_table.md").write_text("PWNED\n")
    body = pr_evidence.append_ui_section(
        "text", ui_dir, repo=UI_REPO, pr=UI_PR, head_sha=UI_SHA
    )
    assert "PWNED" not in body
    assert "## Web UI" in body
    assert (
        "https://raw.githubusercontent.com/example-owner/example-repo/"
        f"__HOUSESITTER_EVIDENCE_SHA__/pr/7/{UI_SHA}/typical__index-phone__head.png"
        in body
    )


def test_ui_section_drops_hostile_manifest_entries(tmp_path):
    manifest = _ui_manifest(
        _ui_capture(),
        _ui_capture(
            name="../../evil.png",
            fixture='"><img src="https://evil.example/y.png">',
            status="pwned",
            head_problems=["console.error: planted"],
        ),
    )
    ui_dir = _ui_dir_with_manifest(tmp_path, manifest)
    body = pr_evidence.append_ui_section(
        "text", ui_dir, repo=UI_REPO, pr=UI_PR, head_sha=UI_SHA
    )
    assert "evil.example" not in body
    assert "../../evil.png" not in body
    assert "| pwned |" not in body
    assert "planted" not in body
    assert "typical__index-phone__head.png" in body
    assert "invalid manifest entries" in body


def test_ui_section_neutralises_hostile_problems_on_a_valid_capture(tmp_path):
    manifest = _ui_manifest(
        _ui_capture(
            head_problems=["![](https://evil.example/z.png) | <b>boom</b>\n# nope"],
        ),
    )
    ui_dir = _ui_dir_with_manifest(tmp_path, manifest)
    body = pr_evidence.append_ui_section(
        "text", ui_dir, repo=UI_REPO, pr=UI_PR, head_sha=UI_SHA
    )
    # The problem text stays readable, but pipes/newlines are neutralised and
    # the whole cell is a code span, so no markup, image, or heading renders.
    assert "`![](https://evil.example/z.png) / <b>boom</b> # nope`" in body
    assert "\n# nope" not in body


def test_ui_check_failures_add_a_top_line_notice(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.0.0", VERSION)
    manifest = _ui_manifest(
        _ui_capture(head_problems=["console.error: boom"]),
    )
    ui_dir = _ui_dir_with_manifest(tmp_path, manifest)
    body = pr_evidence.build_comment(
        base, head, ui_dir, repo=UI_REPO, pr=UI_PR, head_sha=UI_SHA
    )
    assert "**Web UI checks failed" in body
    assert body.index("**Web UI checks failed") < body.index("## Before / after evidence") + 400
    assert "console.error: boom" in body


def test_ui_section_without_failures_has_no_notice(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.0.0", VERSION)
    ui_dir = _ui_dir_with_manifest(tmp_path, _ui_manifest(_ui_capture()))
    body = pr_evidence.build_comment(
        base, head, ui_dir, repo=UI_REPO, pr=UI_PR, head_sha=UI_SHA
    )
    assert "Web UI checks failed" not in body


def test_ui_patterns_mirror_ui_evidence():
    ui_spec = importlib.util.spec_from_file_location(
        "ui_evidence", ROOT / "scripts" / "ui_evidence.py"
    )
    ui_evidence = importlib.util.module_from_spec(ui_spec)
    ui_spec.loader.exec_module(ui_evidence)
    assert pr_evidence.UI_CAPTURE_RE.pattern == ui_evidence.CAPTURE_RE.pattern
    assert pr_evidence.SHA_PLACEHOLDER == ui_evidence.SHA_PLACEHOLDER
    assert pr_evidence.MAX_UI_TEXT_LEN == ui_evidence.MAX_PROBLEM_LEN


def test_hostile_scenario_id_cannot_break_out_of_its_code_span(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", [])
    head = make_tree(
        tmp_path, "head", "1.0.0", [{"id": "o`k", "title": "T", "argv": ["--version"]}]
    )
    body = pr_evidence.build_comment(base, head)
    assert pr_evidence.code_span("o`k") in body
    assert "| `o`k` |" not in body


# Browser provenance (issue #8) and the empty table (issue #9) -----------------
#
# The manifest records which browser captured (CI: Google Chrome; local:
# Playwright's bundled Chromium). The publisher treats that key as hostile
# data: unknown channels and bad versions render a neutral sentence, never
# the bad value. With nothing captured, there is no table header.


INTRO_SENTENCE = (
    "Fixture house only. `scripts/ui_evidence.py` captures every fixture in "
    "`tests/fixtures/dashboard/` at phone (360x780), desktop (1280x900, light "
    "and dark), and e-ink (800x480) sizes."
)
IMAGES_SENTENCE = (
    "Images live on the `pr-evidence` branch, pinned to the evidence push, "
    "so they never change afterwards."
)
BROWSER_UNKNOWN = "The manifest does not record a recognised browser."


def _ui_manifest_with_browser(browser, *captures):
    manifest = _ui_manifest(*captures)
    manifest["browser"] = browser
    return manifest


def test_ui_section_names_the_capturing_browser():
    manifest = _ui_manifest_with_browser(
        {"channel": "chrome", "version": "131.0.6778"}, _ui_capture()
    )
    full, table, failures = pr_evidence.render_ui_section(
        manifest, UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert INTRO_SENTENCE in full and INTRO_SENTENCE in table
    captured = "Captured headless with Google Chrome 131.0.6778. " + IMAGES_SENTENCE
    assert captured in full and captured in table
    assert BROWSER_UNKNOWN not in full


def test_ui_section_names_bundled_chromium_for_the_default_channel():
    manifest = _ui_manifest_with_browser(
        {"channel": "chromium", "version": "141.0.7390"}, _ui_capture()
    )
    full, _, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert "Captured headless with Playwright's bundled Chromium 141.0.7390." in full


def test_ui_section_without_a_browser_key_is_neutral():
    # An old manifest has no "browser" key: the capture still renders, with
    # the neutral sentence instead of a browser claim.
    full, table, _ = pr_evidence.render_ui_section(
        _ui_manifest(_ui_capture()), UI_REPO, UI_PR, UI_SHA
    )
    assert BROWSER_UNKNOWN in full and BROWSER_UNKNOWN in table
    assert IMAGES_SENTENCE in full
    assert "Captured headless" not in full
    assert "typical__index-phone__head.png" in full


HOSTILE_BROWSERS = [
    {"channel": "chrome<script>", "version": "131.0.6778"},
    {"channel": "chrome", "version": "1.2.3.4.5"},
    {"channel": "chrome", "version": "9" * 40},
    {"channel": "chrome", "version": 131},
    {"channel": "chrome"},
    {"channel": ["chrome"], "version": "131.0.6778"},
    {"channel": "firefox", "version": "131.0.6778"},
    ["chrome", "131.0.6778"],
    "chrome",
]


@pytest.mark.parametrize("browser", HOSTILE_BROWSERS)
def test_ui_section_never_echoes_a_hostile_browser(browser):
    manifest = _ui_manifest_with_browser(browser, _ui_capture())
    full, table, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert BROWSER_UNKNOWN in full and BROWSER_UNKNOWN in table
    assert "Captured headless" not in full
    if isinstance(browser, dict):
        for value in browser.values():
            if isinstance(value, str):
                assert value not in full and value not in table
    elif isinstance(browser, str):
        assert browser not in full
    # A bad browser value never costs the valid capture its table row.
    assert "typical__index-phone__head.png" in full
    assert "| Capture | Status | Checks |" in table


def test_ui_section_without_captures_has_no_table_header():
    full, table, failures = pr_evidence.render_ui_section(
        {"version": 1, "browser": None, "captures": []}, UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert "nothing to show" in full and "nothing to show" in table
    assert "| Capture | Status | Checks |" not in full
    assert "| Capture | Status | Checks |" not in table
    assert "Captured headless" not in full
    assert IMAGES_SENTENCE not in full
    assert BROWSER_UNKNOWN not in full


def test_ui_section_with_only_invalid_captures_omits_the_table():
    manifest = _ui_manifest_with_browser(
        {"channel": "chrome", "version": "131.0.6778"},
        _ui_capture(status="pwned", head="not a capture name"),
    )
    full, table, failures = pr_evidence.render_ui_section(
        manifest, UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert "**Omitted 1 UI capture(s) with invalid manifest entries.**" in full
    assert "| Capture | Status | Checks |" not in full
    assert "| Capture | Status | Checks |" not in table
    # Nothing validated, so there is no capture to describe.
    assert "Captured headless" not in full
    assert IMAGES_SENTENCE not in full
    assert BROWSER_UNKNOWN not in full
    # "Nothing to show" is for an empty captures list only, never together
    # with the omission note.
    assert "nothing to show" not in full


def test_ui_intro_wording_matches_ui_evidence():
    ui_spec = importlib.util.spec_from_file_location(
        "ui_evidence", ROOT / "scripts" / "ui_evidence.py"
    )
    ui_evidence = importlib.util.module_from_spec(ui_spec)
    ui_spec.loader.exec_module(ui_evidence)
    assert pr_evidence.UI_INTRO == ui_evidence.UI_INTRO
    assert pr_evidence.UI_IMAGES_NOTE == ui_evidence.UI_IMAGES_NOTE
    assert pr_evidence.UI_BROWSER_UNKNOWN == ui_evidence.UI_BROWSER_UNKNOWN
    assert pr_evidence.UI_BROWSER_NAMES == ui_evidence.UI_BROWSER_NAMES
    assert (
        pr_evidence.UI_BROWSER_VERSION_RE.pattern
        == ui_evidence.UI_BROWSER_VERSION_RE.pattern
    )
    assert (
        pr_evidence.UI_BROWSER_VERSION_MAX_LEN
        == ui_evidence.UI_BROWSER_VERSION_MAX_LEN
    )
    # The rendered paragraphs are identical too, not just the constants.
    manifest = _ui_manifest_with_browser(
        {"channel": "chrome", "version": "131.0.6778"}, _ui_capture()
    )
    pr_full, _, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    ui_full, _ = ui_evidence.build_ui_section(
        manifest, UI_REPO, UI_PR, UI_SHA, 60_000, True
    )
    captured = "Captured headless with Google Chrome 131.0.6778. " + IMAGES_SENTENCE
    assert INTRO_SENTENCE in pr_full and INTRO_SENTENCE in ui_full
    assert captured in pr_full and captured in ui_full


@pytest.mark.parametrize(
    "channel, version, shown",
    [
        ("chrome", "131.0.6778.85", "Google Chrome 131.0.6778.85"),
        ("chrome-beta", "132.0.6834", "Google Chrome Beta 132.0.6834"),
        ("msedge", "131.0.2903", "Microsoft Edge 131.0.2903"),
        ("chrome", "131", "Google Chrome 131"),
    ],
)
def test_ui_section_names_every_allow_listed_browser(channel, version, shown):
    manifest = _ui_manifest_with_browser(
        {"channel": channel, "version": version}, _ui_capture()
    )
    full, _, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert f"Captured headless with {shown}. " + IMAGES_SENTENCE in full


def test_ui_section_with_a_null_browser_and_captures_is_neutral():
    manifest = _ui_manifest_with_browser(None, _ui_capture())
    full, table, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert BROWSER_UNKNOWN + " " + IMAGES_SENTENCE in full
    assert "typical__index-phone__head.png" in full


def test_ui_section_tolerates_a_capture_without_head_problems():
    # Validation treats a missing head_problems as no problems; rendering
    # must agree instead of raising KeyError and killing the publisher.
    capture = _ui_capture()
    del capture["head_problems"]
    full, table, failures = pr_evidence.render_ui_section(
        _ui_manifest(capture), UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert "| `typical / index-phone` | added | `pass` |" in table


def test_ui_diff_unavailable_reason_cannot_render_markdown():
    manifest = _ui_manifest(
        _ui_capture(
            status="changed",
            base="typical__index-phone__base.png",
            diff=None,
            diff_error="[evil](https://evil.example/x) *pwn*",
        )
    )
    full, _, failures = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert failures == 1
    assert "Diff unavailable: `[evil](https://evil.example/x) *pwn*`." in full
    assert "Diff unavailable: [evil]" not in full


@pytest.mark.parametrize(
    "version, named", [("9" * 32, True), ("9" * 33, False)]
)
def test_ui_browser_version_length_boundary(version, named):
    manifest = _ui_manifest_with_browser(
        {"channel": "chrome", "version": version}, _ui_capture()
    )
    full, _, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert (f"Captured headless with Google Chrome {version}." in full) is named
    assert (BROWSER_UNKNOWN in full) is not named
    if not named:
        assert version not in full


def test_ui_section_with_a_list_browser_echoes_nothing():
    manifest = _ui_manifest_with_browser(["chrome", "131.0.6778"], _ui_capture())
    full, table, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert BROWSER_UNKNOWN in full and BROWSER_UNKNOWN in table
    assert "131.0.6778" not in full


@pytest.mark.parametrize("problems", [123, "x", [123], None])
def test_ui_capture_with_bad_head_problems_is_dropped(problems):
    full, table, failures = pr_evidence.render_ui_section(
        _ui_manifest(_ui_capture(head_problems=problems)), UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert "**Omitted 1 UI capture(s) with invalid manifest entries.**" in full
    assert "| Capture | Status | Checks |" not in table


def test_ui_section_mixing_valid_and_invalid_captures():
    manifest = _ui_manifest_with_browser(
        {"channel": "chrome", "version": "131.0.6778"},
        _ui_capture(),
        _ui_capture(status="pwned"),
    )
    full, table, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert "Captured headless with Google Chrome 131.0.6778." in full
    assert "| `typical / index-phone` | added | `pass` |" in table
    assert "**Omitted 1 UI capture(s) with invalid manifest entries.**" in full
    assert "nothing to show" not in full


def test_ui_section_with_valid_captures_has_neither_empty_note():
    full, table, _ = pr_evidence.render_ui_section(
        _ui_manifest_with_browser(None, _ui_capture()), UI_REPO, UI_PR, UI_SHA
    )
    assert BROWSER_UNKNOWN + " " + IMAGES_SENTENCE in table
    assert "nothing to show" not in full
    assert "Omitted" not in full


def test_ui_section_without_captures_has_no_omission_note():
    full, _, _ = pr_evidence.render_ui_section(
        {"version": 1, "captures": []}, UI_REPO, UI_PR, UI_SHA
    )
    assert "nothing to show" in full
    assert "Omitted" not in full


# Removed captures (issue #21) ------------------------------------------------
#
# A removed capture keeps its base PNG on the pr-evidence branch, so the
# comment shows the before image with no after or diff image.


REMOVED_SENTENCE = "Removed page: no after image (the head no longer has this page)."
ADDED_SENTENCE = "New page: no before image (the base doesn't have this page)."


def _removed_capture():
    return _ui_capture(
        status="removed",
        base="typical__index-phone__base.png",
        head=None,
        diff=None,
    )


def test_ui_section_shows_removed_capture_with_before_image_only():
    full, table, _ = pr_evidence.render_ui_section(
        _ui_manifest(_removed_capture()), UI_REPO, UI_PR, UI_SHA
    )
    assert "| `typical / index-phone` | removed |" in table
    assert "<details><summary>Screenshots</summary>" in full
    assert REMOVED_SENTENCE in full
    assert (
        "https://raw.githubusercontent.com/"
        f"{UI_REPO}/__HOUSESITTER_EVIDENCE_SHA__/pr/{UI_PR}/{UI_SHA}/"
        "typical__index-phone__base.png" in full
    )
    assert 'alt="before: typical / index-phone"' in full
    assert "after: typical / index-phone" not in full
    assert "diff: typical / index-phone" not in full
    assert "Diff unavailable" not in full


def test_ui_section_with_only_unchanged_captures_has_no_details():
    # Honest unchanged captures carry no file names (build_manifest deletes
    # the PNGs and records None for base/head/diff).
    manifest = _ui_manifest(
        _ui_capture(
            status="unchanged",
            base=None,
            head=None,
            diff=None,
        )
    )
    full, table, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert "| `typical / index-phone` | unchanged |" in table
    assert "<details>" not in full


def test_ui_added_sentence_names_the_page_not_the_web_dir():
    full, _, _ = pr_evidence.render_ui_section(
        _ui_manifest(_ui_capture()), UI_REPO, UI_PR, UI_SHA
    )
    assert ADDED_SENTENCE in full
    assert "the base has no `web/`" not in full


# Publisher validation: forbidden fields (adversarial review) -----------------
#
# added forbids base and diff; removed forbids head and diff; unchanged
# forbids base, head and diff. Added and removed forbid a non-None
# diff_error; removed forbids non-empty head_problems. A violating entry is
# dropped and counted in the omission note, like every other invalid entry.


def _changed_capture():
    return _ui_capture(
        status="changed",
        base="typical__index-phone__base.png",
        head="typical__index-phone__head.png",
        diff="typical__index-phone__diff.png",
    )


@pytest.mark.parametrize(
    "capture",
    [
        # removed must not name a head or diff file
        _ui_capture(
            status="removed",
            base="typical__index-phone__base.png",
            head="typical__index-phone__head.png",
            diff=None,
        ),
        _ui_capture(
            status="removed",
            base="typical__index-phone__base.png",
            head=None,
            diff="typical__index-phone__diff.png",
        ),
        # added must not name a base file
        _ui_capture(
            status="added",
            base="typical__index-phone__base.png",
            head="typical__index-phone__head.png",
            diff=None,
        ),
        # unchanged must not name any file
        _ui_capture(
            status="unchanged",
            base="typical__index-phone__base.png",
            head=None,
            diff=None,
        ),
        _ui_capture(
            status="unchanged",
            base=None,
            head="typical__index-phone__head.png",
            diff=None,
        ),
        _ui_capture(
            status="unchanged",
            base=None,
            head=None,
            diff="typical__index-phone__diff.png",
        ),
    ],
)
def test_ui_section_drops_captures_with_forbidden_file_names(capture):
    full, table, failures = pr_evidence.render_ui_section(
        _ui_manifest(capture), UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 0
    assert "**Omitted 1 UI capture(s) with invalid manifest entries.**" in full
    assert "| Capture | Status | Checks |" not in table
    assert "<img" not in full
    assert "typical__index-phone__base.png" not in full
    assert "typical__index-phone__head.png" not in full
    assert "typical__index-phone__diff.png" not in full


@pytest.mark.parametrize(
    "capture",
    [
        _ui_capture(
            status="removed",
            base="typical__index-phone__base.png",
            head=None,
            diff=None,
            diff_error="diff unavailable: boom",
        ),
        _ui_capture(
            status="removed",
            base="typical__index-phone__base.png",
            head=None,
            diff=None,
            head_problems=["console.error: boom"],
        ),
        _ui_capture(
            status="added",
            base=None,
            head="typical__index-phone__head.png",
            diff=None,
            diff_error="diff unavailable: boom",
        ),
        # A diff error means no diff image: both at once is contradictory.
        _ui_capture(
            status="changed",
            base="typical__index-phone__base.png",
            head="typical__index-phone__head.png",
            diff="typical__index-phone__diff.png",
            diff_error="diff unavailable: boom",
        ),
        # Unchanged pairs are equal pixels, so a diff never failed.
        _ui_capture(
            status="unchanged",
            base=None,
            head=None,
            diff=None,
            diff_error="diff unavailable: boom",
        ),
    ],
)
def test_ui_section_drops_captures_with_forbidden_check_fields(capture):
    full, table, failures = pr_evidence.render_ui_section(
        _ui_manifest(capture), UI_REPO, UI_PR, UI_SHA
    )
    # A hostile removed/added entry must not count toward the failure banner.
    assert failures == 0
    assert "**Omitted 1 UI capture(s) with invalid manifest entries.**" in full
    assert "| Capture | Status | Checks |" not in table
    assert "boom" not in full
    assert "<img" not in full


def test_ui_section_keeps_honest_changed_with_diff_error():
    manifest = _ui_manifest(
        _ui_capture(
            status="changed",
            base="typical__index-phone__base.png",
            head="typical__index-phone__head.png",
            diff=None,
            diff_error="diff unavailable: boom",
        )
    )
    full, table, failures = pr_evidence.render_ui_section(
        manifest, UI_REPO, UI_PR, UI_SHA
    )
    assert failures == 1
    assert "| `typical / index-phone` | changed |" in table
    assert "typical__index-phone__base.png" in full
    assert "typical__index-phone__head.png" in full
    assert "Diff unavailable:" in full


def test_select_ui_captures_reports_exactly_the_pictured_files():
    manifest = _ui_manifest(
        _ui_capture(fixture="added-fix", head="added-fix__index-phone__head.png"),
        _ui_capture(
            fixture="removed-fix",
            view="index-desktop",
            status="removed",
            base="removed-fix__index-desktop__base.png",
            head=None,
            diff=None,
        ),
        _ui_capture(
            fixture="changed-fix",
            view="eink",
            status="changed",
            base="changed-fix__eink__base.png",
            head="changed-fix__eink__head.png",
            diff="changed-fix__eink__diff.png",
        ),
        _ui_capture(
            status="changed",
            base="typical__index-desktop__base.png",
            head="typical__index-desktop__head.png",
            diff=None,
            diff_error="diff unavailable: boom",
            fixture="other",
            view="index-desktop",
        ),
        _ui_capture(
            fixture="same-fix",
            status="unchanged",
            base=None,
            head=None,
            diff=None,
        ),
    )
    kept, dropped = pr_evidence.select_ui_captures(manifest)
    assert dropped == 0
    pictured = {}
    for cap, files in kept:
        pictured[cap["fixture"] + "/" + cap["view"]] = files
    assert pictured["added-fix/index-phone"] == ["added-fix__index-phone__head.png"]
    assert pictured["removed-fix/index-desktop"] == ["removed-fix__index-desktop__base.png"]
    assert pictured["changed-fix/eink"] == [
        "changed-fix__eink__base.png",
        "changed-fix__eink__head.png",
        "changed-fix__eink__diff.png",
    ]
    assert pictured["other/index-desktop"] == [
        "typical__index-desktop__base.png",
        "typical__index-desktop__head.png",
    ]
    assert pictured["same-fix/index-phone"] == []


# Part 1 (Decision 13, option A): the publisher rebuilds the text section
# from a structured results file instead of posting the PR's markdown. ---


FORGED = "Tests: all passed. Approved by maintainer @jonschwenn <img src=x>"


def _results_doc(**overrides):
    doc = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "head_sha": "c" * 40},
        "scenarios": [
            {
                "id": "version",
                "title": "Installed version",
                "command": ["housesitter", "--version"],
                "exit_before": 0,
                "exit_after": 0,
                "expected_exit": 0,
                "size_before": 18,
                "size_after": 60,
                "changed": True,
                "before": "$ exit 0\n--- stdout ---\nhousesitter 1.0.0\n",
                "after": "$ exit 0\n--- stdout ---\n" + FORGED + "\n",
            }
        ],
    }
    doc.update(overrides)
    return doc


def _strip_fences(text):
    """Remove every fenced code block; what is left renders as Markdown."""
    out = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "".join(out)


def test_build_results_records_changed_and_new(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.1.0", VERSION)
    results = pr_evidence.build_results(base, head)
    assert results["version"] == 1
    (entry,) = results["scenarios"]
    assert entry["id"] == "version"
    assert entry["command"] == ["housesitter", "--version"]
    assert entry["changed"] is True
    assert entry["new_in_pr"] is False
    assert "1.0.0" in entry["before"]
    assert "1.1.0" in entry["after"]
    # A base without a package has no "before" side.
    bare = make_tree(tmp_path, "bare", None, VERSION)
    results = pr_evidence.build_results(bare, head)
    (entry,) = results["scenarios"]
    assert entry["before"] is None
    assert entry["exit_before"] is None


def test_render_from_results_matches_build_comment_sections(tmp_path):
    base = make_tree(tmp_path, "base", "1.0.0", VERSION)
    head = make_tree(tmp_path, "head", "1.1.0", VERSION)
    results = pr_evidence.build_results(base, head, head_sha="d" * 40)
    assert results["metadata"]["head_sha"] == "d" * 40
    rebuilt = pr_evidence.render_text_from_results(results, "d" * 40)
    classic = pr_evidence.build_comment(base, head)
    for needle in (
        "| `version` | changed |",
        "-housesitter 1.0.0",
        "+housesitter 1.1.0",
        "Full before",
        "Full after",
    ):
        assert needle in rebuilt, needle
        assert needle in classic, needle


def test_forged_approval_appears_only_inside_fences():
    body = pr_evidence.render_text_from_results(_results_doc(), "c" * 40)
    assert body.startswith(pr_evidence.MARKER)
    assert "Approved by maintainer" in body  # the evidence itself is preserved for review
    rendered = _strip_fences(body)
    assert "Approved by maintainer" not in rendered
    assert "@jonschwenn" not in rendered
    assert "<img" not in rendered


def test_render_from_results_neutralises_mentions_and_html():
    body = pr_evidence.render_text_from_results(_results_doc(), "c" * 40)
    assert "@jonschwenn" not in body  # broken up so it cannot ping
    assert "<img" not in body  # broken up so no tag survives, fences or not


def test_render_from_results_calls_out_unexpected_exits():
    doc = _results_doc()
    doc["scenarios"][0]["exit_after"] = 2
    doc["scenarios"][0]["expected_exit"] = 0
    body = pr_evidence.render_text_from_results(doc, "c" * 40)
    assert "**Unexpected exit:** `version` exited 2, expected 0." in body


def test_render_from_results_lists_removed_scenarios():
    doc = _results_doc()
    doc["scenarios"] = []
    doc["removed"] = ["help"]
    body = pr_evidence.render_text_from_results(doc, "c" * 40)
    assert "| `help` | removed |" in body


def test_render_from_results_marks_author_attested():
    body = pr_evidence.render_text_from_results(_results_doc(), "c" * 40)
    assert "produced by the PR's own CI run" in body


def test_cropped_capture_shows_a_warning(tmp_path):
    manifest = _ui_manifest(
        _ui_capture(
            status="changed",
            base="typical__index-phone__base.png",
            head="typical__index-phone__head.png",
            diff="typical__index-phone__diff.png",
            crop_px=4000,
        )
    )
    full, _, _ = pr_evidence.render_ui_section(manifest, UI_REPO, UI_PR, UI_SHA)
    assert "Cropped at 4,000 px to fit the size limit" in full
    assert "changes below this line are not shown" in full


def test_invalid_crop_px_drops_the_capture():
    manifest = _ui_manifest(_ui_capture(crop_px="4000"))
    kept, dropped = pr_evidence.select_ui_captures(manifest)
    assert (kept, dropped) == ([], 1)
    manifest = _ui_manifest(_ui_capture(crop_px=1234))
    kept, dropped = pr_evidence.select_ui_captures(manifest)
    assert (kept, dropped) == ([], 1)


def test_valid_crop_px_values_are_kept():
    for height in (6000, 4000, 3000):
        manifest = _ui_manifest(_ui_capture(crop_px=height))
        kept, dropped = pr_evidence.select_ui_captures(manifest)
        assert dropped == 0
        assert kept[0][0]["crop_px"] == height


def test_crop_height_steps_match_ui_evidence():
    ui_spec = importlib.util.spec_from_file_location(
        "ui_evidence", ROOT / "scripts" / "ui_evidence.py"
    )
    ui_evidence = importlib.util.module_from_spec(ui_spec)
    ui_spec.loader.exec_module(ui_evidence)
    assert ui_evidence.CROP_STEPS[0] == ui_evidence.MAX_SHOT_HEIGHT_PX
    assert list(pr_evidence.UI_CROP_HEIGHTS) == list(ui_evidence.CROP_STEPS[1:])


# Fence escape (review round 1, finding 1) ------------------------------------
#
# The closing fence used to be appended directly after text that might not
# end in a newline, so it landed inside the last content line and the next
# block's opening fence closed the diff: PR text then rendered as a heading,
# image or link. Every PR-supplied string must lie between an opening fence
# line and its matching closing fence line.


def _fence_states(text):
    """Mark each CommonMark line of ``text`` as inside a fenced code block.

    Splits on the three line endings CommonMark recognises (LF, CRLF, lone
    CR) and tracks backtick/tilde fences: an opening fence is up to three
    spaces plus three or more of one mark (a backtick fence's info string
    must hold no backtick); a closing fence is the same mark, at least as
    long, followed only by spaces. Returns (lines, states); raises when a
    fence is left open.
    """
    lines = re.split(r"\r\n|\r|\n", text)
    states = []
    fence_mark = None
    fence_len = 0
    for line in lines:
        stripped = line[3:] if line.startswith("   ") else line.lstrip(" ")
        if len(line) - len(line.lstrip(" ")) > 3:
            stripped = None  # four-space indent is not a fence
        mark, run, rest = None, 0, ""
        if stripped is not None and stripped[:1] in ("`", "~"):
            mark = stripped[0]
            run = len(stripped) - len(stripped.lstrip(mark))
            rest = stripped[run:]
        if fence_mark is None:
            inside = False
            if mark is not None and run >= 3 and (mark == "~" or "`" not in rest):
                fence_mark, fence_len = mark, run
        else:
            inside = True
            if mark == fence_mark and run >= fence_len and rest.strip(" \t") == "":
                fence_mark = None
        states.append(inside)
    assert fence_mark is None, f"unbalanced fences in: {text[-200:]!r}"
    return lines, states


def _assert_only_fenced(body, needles):
    """Every line holding a PR-derived needle must sit inside a fence."""
    lines, states = _fence_states(body)
    for lineno, line in enumerate(lines):
        for needle in needles:
            assert not (needle in line and not states[lineno]), (
                f"PR-derived text renders outside code on line {lineno}: {line!r}"
            )


def test_fence_closes_on_its_own_line():
    assert pr_evidence.fence("abc") == "```\nabc\n```\n"
    assert pr_evidence.fence("abc\n") == "```\nabc\n```\n"
    assert pr_evidence.fence("abc\r\n") == "```\nabc\r\n```\n"
    assert pr_evidence.fence("abc\r") == "```\nabc\r```\n"
    assert pr_evidence.fence("", "diff") == "```diff\n```\n"
    fenced = pr_evidence.fence("has ``` inside", "diff")
    assert fenced.startswith("````diff\n")
    assert fenced.endswith("\nhas ``` inside\n````\n")


def test_forged_markdown_without_trailing_newline_stays_fenced():
    doc = _results_doc()
    doc["scenarios"][0]["before"] = (
        "## Approved\n\n![x](https://example.com/i)\n\n"
        "[click](https://example.com)\n\nend"
    )
    doc["scenarios"][0]["after"] = "CHANGED-TOKEN"
    body = pr_evidence.render_text_from_results(doc, "c" * 40)
    _assert_only_fenced(
        body,
        ["Approved", "example.com/i", "example.com)", "click", "end", "CHANGED-TOKEN"],
    )


def test_fences_survive_hostile_backticks_tildes_and_newlines():
    rng = random.Random(20261111)
    alphabet = ["`", "``", "~~~~", "#", "![x](https://example.com/i)", "\n", "\r\n", "\r", " "]
    tokens = ["APPROVED-TOKEN", "IMG-TOKEN", "CLICK-TOKEN"]
    for trial in range(50):
        parts = [rng.choice(alphabet) for _ in range(rng.randint(1, 12))]
        hostile = "".join(parts + [tokens[trial % 3]] + parts)
        if rng.choice([True, False]):
            hostile += rng.choice(["\n", "\r\n", "\r"])
        doc = _results_doc()
        doc["scenarios"][0]["before"] = hostile
        doc["scenarios"][0]["after"] = hostile + rng.choice(["", "tail"])
        body = pr_evidence.render_text_from_results(doc, "c" * 40)
        _assert_only_fenced(body, tokens)


# Review round 1, finding 9: labels never render as prose. ---------------------
#
# The manifest fixture label used to render inside <summary> as prose, so
# {"fixture": "Approved by maintainer"} read as the bot's own words. Every
# PR-derived value in the rebuilt comment must sit inside a fenced block, a
# code span, an HTML tag, or be replaced by fixed text.


def _code_span_regions(line):
    """(start, end) ranges of `...` code spans on one line."""
    regions = []
    i, n = 0, len(line)
    while i < n:
        if line[i] != "`":
            i += 1
            continue
        j = i
        while j < n and line[j] == "`":
            j += 1
        ticks = line[i:j]
        k = line.find(ticks, j)
        if k == -1:
            break  # unmatched ticks: the rest renders as prose
        regions.append((i, k + len(ticks)))
        i = k + len(ticks)
    return regions


def _assert_no_prose_leak(body, needles):
    """Every PR-derived needle renders only in fences, code spans or tags."""
    lines, states = _fence_states(body)
    for lineno, line in enumerate(lines):
        if states[lineno]:
            continue
        regions = _code_span_regions(line)
        masked = re.sub(r"<[^<>]*>", lambda match: " " * len(match.group(0)), line)
        for needle in needles:
            start = 0
            while True:
                at = masked.find(needle, start)
                if at == -1:
                    break
                assert any(
                    s <= at and at + len(needle) <= e for s, e in regions
                ), f"PR-derived text renders as prose on line {lineno}: {line!r}"
                start = at + 1


def test_hostile_labels_never_render_as_prose_in_text_section():
    doc = _results_doc()
    entry = doc["scenarios"][0]
    entry["id"] = "evil-`id`"
    entry["title"] = "Approved by maintainer"
    entry["command"] = ["housesitter", "--say", "[click](https://example.com/pwn)"]
    body = pr_evidence.render_text_from_results(doc, "c" * 40)
    _assert_no_prose_leak(
        body, ["evil-", "Approved by maintainer", "example.com/pwn"]
    )


def test_hostile_fixture_label_never_renders_as_prose():
    capture = _ui_capture(
        fixture="Approved by maintainer",
        view="index-phone",
        status="changed",
        base="typical__index-phone__base.png",
        head="typical__index-phone__head.png",
        diff="typical__index-phone__diff.png",
        head_problems=["[click](https://example.com/pwn)"],
    )
    full, table, _ = pr_evidence.render_ui_section(
        _ui_manifest(capture), UI_REPO, UI_PR, UI_SHA
    )
    _assert_no_prose_leak(
        full + "\n" + table, ["Approved by maintainer", "example.com/pwn"]
    )


# Fake-controller evidence (Decision 9, option A) ---------------------------
#
# The unifi_wifi scenarios run the installed CLI against scripts/fake_unifi.py
# (the HEAD tree's copy, serving the HEAD tree's fixtures) with a written
# config plus secrets file, exactly as a user would set it up.


def _repo_fake_scenarios():
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    fakes = [scenario for scenario in scenarios if scenario.get("fake")]
    assert len(fakes) >= 6, "the unifi_wifi evidence scenarios are missing"
    return fakes, set(volatile_keys)


def test_fake_scenarios_are_deterministic(tmp_path):
    require_test_tool(openssl is not None, "openssl is not installed")
    fakes, volatile = _repo_fake_scenarios()
    certdir = tempfile.mkdtemp(prefix="hs-evidence-test-cert-", dir=str(tmp_path))
    for scenario in fakes:
        spec = pr_evidence.validate_fake_spec(scenario["fake"], scenario["id"])
        cert_paths = pr_evidence.make_cert(certdir)
        try:
            with pr_evidence.run_fake(ROOT, spec, *cert_paths) as first:
                first_run = pr_evidence.run_scenario(ROOT, scenario, first, volatile)
            with pr_evidence.run_fake(ROOT, spec, *cert_paths) as second:
                second_run = pr_evidence.run_scenario(ROOT, scenario, second, volatile)
        except pr_evidence._SkipScenario as exc:
            require_test_tool(False, str(exc))
            continue
        assert first_run["exit"] == second_run["exit"] == scenario.get("expect_exit", 0)
        first_text = pr_evidence.render(first_run, volatile)
        second_text = pr_evidence.render(second_run, volatile)
        assert first_text == second_text, scenario["id"]
        # Neither run's endpoint may reach the output: the port is per-run.
        assert f"127.0.0.1:{first.port}" not in first_text
        assert f"127.0.0.1:{second.port}" not in second_text
        if scenario["id"] in (
            "unifi-wifi-snapshot-typical",
            "unifi-wifi-snapshot-empty",
            "unifi-wifi-snapshot-large",
            "unifi-wifi-get-saved",
        ):
            # Snapshots carry collected_at and saved ids, so the redaction
            # must have fired: without it this equality would prove nothing.
            assert first_text.count(pr_evidence.VOLATILE) >= 2, scenario["id"]
        else:
            assert "upstream_auth" in first_text or "upstream_tls" in first_text


def test_volatile_shaped_client_name_is_preserved(monkeypatch):
    # A real client name equal to the envelope's collected_at must survive
    # verbatim: redaction applies only to the echoed step header, never to
    # the JSON body (already blanked by volatile_keys).
    volatile = {"collected_at", "saved", "more.params.id"}
    scenario = {
        "id": "collision",
        "argv": ["invoke", "housesitter.unifi_wifi.snapshot"],
        "fake": {"fixture_set": "typical"},
    }
    stamp = "2026-11-15T00:00:00Z"

    def run_with_name(name):
        env = {
            "collected_at": stamp,
            "saved": "snapshot_20261115T000000Z_1234",
            "data": {"clients": [{"name": name}]},
        }
        step = {
            "argv": scenario["argv"],
            "exit": 0,
            "stdout": json.dumps(env),
            "stderr": "",
        }
        monkeypatch.setattr(pr_evidence, "_run_one_step", lambda *a, **k: step)
        fake = pr_evidence.FakeInfo(None, 8443, "AA")
        return pr_evidence.run_scenario(ROOT, scenario, fake, volatile)

    real_text = pr_evidence.render(run_with_name(stamp), volatile)
    other_text = pr_evidence.render(run_with_name(pr_evidence.VOLATILE), volatile)
    assert real_text != other_text
    assert stamp in real_text


def test_step_header_redacts_replayed_ids_but_body_keeps_real_data(monkeypatch):
    # A stepped run replaying $saved_id(0): the echoed argv header must hide
    # the replayed id (determinism), while a real client name equal to that
    # id survives verbatim in the body.
    volatile = {"collected_at", "saved", "more.params.id"}
    scenario = {
        "id": "redact-scope",
        "argv": ["invoke", "housesitter.unifi_wifi.snapshot"],
        "fake": {"fixture_set": "typical"},
        "steps": [
            {"argv": ["invoke", "housesitter.unifi_wifi.snapshot"]},
            {
                "argv": [
                    "invoke",
                    "housesitter.unifi_wifi.get",
                    "--params",
                    '{"id": "$saved_id(0)"}',
                ]
            },
        ],
    }
    saved = "snapshot_20261115T000000Z_0099"
    stamp = "2026-11-15T00:00:00Z"
    calls = {"n": 0}

    def fake_step(tree, argv, home, env):
        calls["n"] += 1
        if calls["n"] == 1:
            env_out = {
                "collected_at": stamp,
                "saved": saved,
                "data": {"clients": [{"name": saved}]},
            }
        else:
            env_out = {
                "collected_at": stamp,
                "saved": saved,
                "data": {"clients": []},
            }
        return {"argv": argv, "exit": 0, "stdout": json.dumps(env_out), "stderr": ""}

    monkeypatch.setattr(pr_evidence, "_run_one_step", fake_step)
    fake = pr_evidence.FakeInfo(None, 8443, "AA")
    text = pr_evidence.render(pr_evidence.run_scenario(ROOT, scenario, fake, volatile), volatile)
    assert saved in text
    headers = [line for line in text.splitlines() if line.startswith("$ housesitter")]
    assert len(headers) == 2
    assert all(saved not in line for line in headers)
    assert pr_evidence.VOLATILE in headers[1]


def test_project_data_counts():
    envelope = {
        "collected_at": "T",
        "saved": "S",
        "truncated": True,
        "more": {"command": "housesitter.unifi_wifi.get", "params": {"id": "abc"}},
        "warnings": ["w1", "w2"],
        "source": "fake",
        "data": {
            "clients": [{"name": "a"}, {"name": "b"}],
            "aps": {"lounge": [{"mac": "x"}]},
            "count": 2,
            "label": "house",
        },
    }
    projected = pr_evidence.project_data_counts(envelope)
    assert projected["data"]["clients"] == "<2 items>"
    assert projected["data"]["aps"] == {"lounge": "<1 items>"}
    assert projected["data"]["count"] == 2
    assert projected["data"]["label"] == "house"
    # Everything outside data renders in full, so paging still diffs.
    assert projected["truncated"] is True
    assert projected["more"] == {
        "command": "housesitter.unifi_wifi.get",
        "params": {"id": "abc"},
    }
    assert projected["warnings"] == ["w1", "w2"]
    assert projected["source"] == "fake"
    # A change in the paging fields survives the projection.
    flipped = dict(envelope, truncated=False)
    assert pr_evidence.project_data_counts(flipped) != projected
    more_params = dict(envelope["more"]["params"], cursor=3)
    moved = dict(envelope, more=dict(envelope["more"], params=more_params))
    assert pr_evidence.project_data_counts(moved) != projected


def test_unknown_render_mode_fails_loudly():
    scenario = {
        "id": "bogus-render",
        "argv": ["invoke", "housesitter.unifi_wifi.snapshot"],
        "fake": {"fixture_set": "typical"},
        "render": "pie-chart",
    }
    fake = pr_evidence.FakeInfo(None, 8443, "AA")
    with pytest.raises(ValueError, match="render"):
        pr_evidence.run_scenario(ROOT, scenario, fake, set())


def test_data_counts_render_mode(monkeypatch):
    volatile = {"collected_at", "saved", "more.params.id"}
    scenario = {
        "id": "counts-mode",
        "argv": ["invoke", "housesitter.unifi_wifi.snapshot"],
        "fake": {"fixture_set": "typical"},
        "render": "data-counts",
        "steps": [
            {"argv": ["invoke", "housesitter.unifi_wifi.snapshot"]},
            {"argv": ["invoke", "$more_command(0)", "--params", "$more_params(0)"]},
        ],
    }
    clients = [{"name": f"client-{i:03d}", "signal_dbm": -60} for i in range(600)]

    def fake_step(tree, argv, home, env, truncated=True):
        if argv[1] == "housesitter.unifi_wifi.snapshot":
            env_out = {
                "collected_at": "2026-11-15T00:00:00Z",
                "saved": "snapshot_20261115T000000Z_1",
                "truncated": True,
                "more": {
                    "command": "housesitter.unifi_wifi.get",
                    "params": {"id": "page_1", "cursor": 0},
                },
                "data": {"clients": clients},
            }
        else:
            env_out = {
                "collected_at": "2026-11-15T00:00:01Z",
                "saved": "snapshot_20261115T000000Z_1",
                "truncated": truncated,
                "data": {"clients": clients[:5]},
            }
        return {"argv": argv, "exit": 0, "stdout": json.dumps(env_out), "stderr": ""}

    raw_sizes = []

    def recording(tree, argv, home, env):
        result = fake_step(tree, argv, home, env)
        raw_sizes.append(len(json.dumps(result["stdout"])) - 2)
        return result

    monkeypatch.setattr(pr_evidence, "_run_one_step", recording)
    fake = pr_evidence.FakeInfo(None, 8443, "AA")
    result = pr_evidence.run_scenario(ROOT, scenario, fake, volatile)
    text = pr_evidence.render(result, volatile)
    assert text.count("(data lists shown as item counts)") == 2
    assert "<600 items>" in text
    assert "<5 items>" in text
    assert "client-042" not in text
    assert '"truncated": true' in text
    assert '"command": "housesitter.unifi_wifi.get"' in text
    # sdk_size stays the raw-stdout SDK measure, not the projection.
    assert result["sdk_size"] == max(raw_sizes)
    assert len(text.encode()) < result["sdk_size"]

    def recording_flipped(tree, argv, home, env):
        return fake_step(tree, argv, home, env, truncated=False)

    monkeypatch.setattr(pr_evidence, "_run_one_step", recording_flipped)
    flipped_text = pr_evidence.render(
        pr_evidence.run_scenario(ROOT, scenario, fake, volatile), volatile
    )
    assert flipped_text != text


def _load_publisher():
    """This tree's privileged publisher with a self-consistent module set.

    publish_evidence does plain "import pr_evidence" (and friends), so its
    dependencies are loaded first under their real names; the returned
    module therefore renders exactly what main's publisher renders.
    """
    scripts = ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    loaded = {}
    for name in (
        "evidence_checks",
        "pr_evidence",
        "test_results",
        "ui_evidence",
        "publish_evidence",
    ):
        spec = importlib.util.spec_from_file_location(name, scripts / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        loaded[name] = module
    return loaded["publish_evidence"]


def _one_line_change(text):
    """A one-line data change, for changed-section sizing."""
    import re as _re

    tweaked, count = _re.subn(
        r'"signal_dbm": (-?\d+)', lambda m: f'"signal_dbm": {int(m.group(1)) - 1}', text, count=1
    )
    if count:
        return tweaked
    lines = text.splitlines(keepends=True)
    return "".join(lines[:-1] + ["# probe variant\n", lines[-1]])


def test_changed_unifi_sections_keep_tests_line_and_screenshots(tmp_path):
    # Every unifi fake scenario changed, before/after differing by one line:
    # the comment must still carry the Tests line, a screenshot, and a
    # small section after the unifi ones (main's shrink order degrades
    # Tests/screenshots before dropping text sections, and keeps a prefix
    # of them, so oversized sections would wipe all three).
    require_test_tool(openssl is not None, "openssl is not installed")
    publish_evidence = _load_publisher()
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    volatile = set(volatile_keys)
    fakes = [scenario for scenario in scenarios if scenario.get("fake")]
    assert len(fakes) >= 6
    by_id = {}
    certdir = tempfile.mkdtemp(prefix="hs-evidence-budget-", dir=str(tmp_path))
    for scenario in fakes:
        spec = pr_evidence.validate_fake_spec(scenario["fake"], scenario["id"])
        cert_paths = pr_evidence.make_cert(certdir)
        try:
            with pr_evidence.run_fake(ROOT, spec, *cert_paths) as fake:
                after = pr_evidence.run_scenario(ROOT, scenario, fake, volatile)
        except pr_evidence._SkipScenario as exc:
            require_test_tool(False, str(exc))
            continue
        after_text = pr_evidence.render(after, volatile)
        by_id[scenario["id"]] = (scenario, after, after_text)
    assert len(by_id) == len(fakes)
    entries = []
    for scenario_id, (scenario, after, after_text) in by_id.items():
        before_text = _one_line_change(after_text)
        assert before_text != after_text
        command = ["housesitter", *scenario["argv"]]
        if scenario.get("fake") is not None and scenario.get("steps"):
            command = []
            for index, step in enumerate(scenario["steps"]):
                if index:
                    command.append("&&")
                command.extend(["housesitter", *step["argv"]])
        entries.append(
            {
                "id": scenario_id,
                "title": scenario.get("title", ""),
                "command": command,
                "exit_before": after["exit"],
                "exit_after": after["exit"],
                "expected_exit": scenario.get("expect_exit", 0),
                "size_before": after["sdk_size"],
                "size_after": after["sdk_size"],
                "changed": True,
                "new_in_pr": False,
                "before": before_text,
                "after": after_text,
            }
        )
    entries.append(
        {
            "id": "zzz-trailing-probe",
            "title": "Small changed output",
            "command": ["housesitter", "--version"],
            "exit_before": 0,
            "exit_after": 0,
            "expected_exit": 0,
            "size_before": 2,
            "size_after": 2,
            "changed": True,
            "new_in_pr": True,
            "before": "a\n",
            "after": "b\n",
        }
    )
    head_sha = "b" * 40
    doc = {
        "version": 1,
        "metadata": {
            "generator": "scripts/pr_evidence.py",
            "base_has_package": True,
            "head_sha": head_sha,
        },
        "scenarios": entries,
        "removed": [],
    }
    top, sections = pr_evidence.text_parts_from_results(doc, head_sha)
    section_bytes = {}
    for section in sections:
        heading = section.split("\n", 1)[0]
        section_bytes[heading] = len(section.encode())

    def section_size(scenario_id):
        (match,) = [
            size
            for heading, size in section_bytes.items()
            if heading.startswith(f"### `{scenario_id}`")
        ]
        return match

    large = section_size("unifi-wifi-snapshot-large")
    get_saved = section_size("unifi-wifi-get-saved")
    typical = section_size("unifi-wifi-snapshot-typical")
    assert large < 12_000, large
    assert get_saved < 12_000, get_saved
    assert typical < 45_000, typical
    artifact = tmp_path / "artifact"
    (artifact / "ui_out").mkdir(parents=True)
    (artifact / "results.json").write_text(json.dumps(doc))
    png_name = "typical__index-phone__head.png"
    (artifact / "ui_out" / png_name).write_bytes(
        publish_evidence.ui_evidence.encode_png(4, 4, 2, bytes([10, 20, 30]) * 16)
    )
    (artifact / "ui_out" / "ui_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "captures": [
                    {
                        "fixture": "typical",
                        "view": "index-phone",
                        "status": "added",
                        "base": None,
                        "head": png_name,
                        "diff": None,
                        "head_problems": [],
                    }
                ],
            }
        )
    )
    results_dir = tmp_path / "test-results" / "test-results-3.9"
    results_dir.mkdir(parents=True)
    cases = "".join(
        f'<testcase classname="tests/test_sample.py" name="test_case_{i:02d}"/>' for i in range(10)
    )
    (results_dir / "junit.xml").write_bytes(
        f'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        f'tests="10" failures="0" errors="0" skipped="0">{cases}</testsuite>'.encode()
    )
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            {
                "total_count": 1,
                "jobs": [{"name": "test (3.9)", "conclusion": "success"}],
            }
        )
    )
    out = tmp_path / "out"
    rc = publish_evidence.main(
        [
            "render",
            "--artifact-dir", str(artifact),
            "--out-dir", str(out),
            "--repo", "example-owner/example-repo",
            "--pr", "7",
            "--head-sha", head_sha,
            "--test-results-dir", str(tmp_path / "test-results"),
            "--jobs-json", str(jobs_path),
        ]
    )
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "**Tests:** 10 passed on 3.9.25." in body
    assert "<img " in body
    assert "### `zzz-trailing-probe`" in body


def test_run_fake_reaps_child_when_startup_is_interrupted(tmp_path, monkeypatch):
    # Everything after a successful Popen is covered by one cleanup finally:
    # an interruption during startup waiting must still reap the child.
    require_test_tool(openssl is not None, "openssl is not installed")
    certdir = tmp_path / "cert"
    certdir.mkdir()
    cert_path, key_path = pr_evidence.make_cert(str(certdir))
    spec = pr_evidence.validate_fake_spec({"fixture_set": "typical"}, "probe")
    children = []
    real_popen = pr_evidence.subprocess.Popen

    def capture(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(pr_evidence.subprocess, "Popen", capture)
    for exc in (KeyboardInterrupt(), OSError("select failed")):
        def boom(*args, _exc=exc, **kwargs):
            raise _exc

        monkeypatch.setattr(pr_evidence, "_wait_for_lines", boom)
        with pytest.raises(type(exc)):
            with pr_evidence.run_fake(ROOT, spec, cert_path, key_path):
                pass  # pragma: no cover - startup never completes
        assert children[-1].poll() is not None


def test_evidence_password_never_leaks(tmp_path):
    require_test_tool(openssl is not None, "openssl is not installed")
    fakes, volatile = _repo_fake_scenarios()
    # The secret really is configured: the run writes it into the fresh HOME.
    home = tmp_path / "home"
    pr_evidence.write_fake_home(
        str(home),
        pr_evidence.FakeInfo(None, 8443, "AA"),
        {"fixture_set": "typical", "reject_login": False, "bad_fingerprint": False},
    )
    secret_file = home / ".config" / "housesitter" / "secrets" / "unifi_password"
    assert secret_file.read_text() == pr_evidence.EVIDENCE_PASSWORD
    assert "PLANTED-SECRET" in pr_evidence.EVIDENCE_PASSWORD
    runs = []
    for scenario in fakes:
        try:
            result = pr_evidence.run_scenario(ROOT, scenario, volatile=volatile)
        except pr_evidence._SkipScenario as exc:
            require_test_tool(False, str(exc))
            continue
        assert not result.get("skipped"), (scenario["id"], result)
        assert pr_evidence.EVIDENCE_PASSWORD not in result["stdout"], scenario["id"]
        assert pr_evidence.EVIDENCE_PASSWORD not in result["stderr"], scenario["id"]
        text = pr_evidence.render(result, volatile)
        assert pr_evidence.EVIDENCE_PASSWORD not in text, scenario["id"]
        runs.append((scenario, result, result))
    # The same string must be absent from the structured results too.
    doc = pr_evidence._results_from_runs(fakes, volatile, fakes, True, runs, None)
    dumped = json.dumps(doc)
    assert pr_evidence.EVIDENCE_PASSWORD not in dumped
    assert "PLANTED-SECRET" not in dumped
    known_ids = {scenario["id"] for scenario in fakes}
    assert evidence_checks.validate_results(doc, known_ids) == []


def test_skipped_scenario_is_reported_not_dropped(tmp_path, monkeypatch):
    require_test_tool(openssl is not None, "openssl is not installed")
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    typical = next(
        scenario for scenario in scenarios if scenario["id"] == "unifi-wifi-snapshot-typical"
    )
    base = make_tree(tmp_path, "base", "1.0.0", [typical])
    head = make_tree(tmp_path, "head", "1.0.0", [typical])

    real_popen = pr_evidence.subprocess.Popen

    def _boom(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        if any("fake_unifi.py" in str(part) for part in argv):
            raise OSError("the network is unavailable")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(pr_evidence.subprocess, "Popen", _boom)
    direct = pr_evidence.run_scenario(ROOT, typical, volatile=set(volatile_keys))
    assert direct.get("skipped")
    assert "the fake controller could not start" in direct["skipped"]
    results = pr_evidence.build_results(base, head)
    (entry,) = results["scenarios"]
    assert entry["exit_before"] == 1
    assert entry["exit_after"] == 1
    # The side labels differ, so the reason renders instead of vanishing
    # into an "unchanged" row.
    assert entry["changed"] is True
    assert "(skipped on base: the fake controller could not start" in entry["before"]
    assert "(skipped on head: the fake controller could not start" in entry["after"]
    assert evidence_checks.validate_results(results, {"unifi-wifi-snapshot-typical"}) == []


def _copy_repo_tree(tmp_path, name):
    """A full working copy of the repo checkout (code, scripts, fixtures)."""
    tree = tmp_path / name
    shutil.copytree(
        ROOT,
        tree,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".hypothesis"
        ),
    )
    return tree


def test_normalise_change_shows_in_snapshot_evidence(tmp_path):
    require_test_tool(openssl is not None, "openssl is not installed")
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    wanted = {"invoke-unifi-wifi-snapshot", "unifi-wifi-snapshot-typical"}
    subset = [scenario for scenario in scenarios if scenario["id"] in wanted]
    assert len(subset) == 2
    base = _copy_repo_tree(tmp_path, "base")
    head = _copy_repo_tree(tmp_path, "head")
    pruned = {"volatile_keys": volatile_keys, "scenarios": subset}
    for tree in (base, head):
        (tree / "tests" / "evidence" / "scenarios.json").write_text(json.dumps(pruned))
    normalise = head / "src" / "housesitter" / "modules" / "unifi_wifi" / "normalise.py"
    text = normalise.read_text()
    old = '"tx_rate_mbps": tx_rate / 1000'
    assert old in text
    normalise.write_text(text.replace(old, '"tx_rate_mbps": tx_rate / 100'))
    results = pr_evidence.build_results(base, head)
    by_id = {entry["id"]: entry for entry in results["scenarios"]}
    assert by_id["unifi-wifi-snapshot-typical"]["changed"] is True
    rebuilt = pr_evidence.render_text_from_results(results, "c" * 40)
    assert "### `unifi-wifi-snapshot-typical`" in rebuilt
    assert "tx_rate_mbps" in rebuilt
    # The old no-config scenario cannot show the change: it only ever
    # proves config_invalid. This is what the evidence showed before.
    assert by_id["invoke-unifi-wifi-snapshot"]["changed"] is False
    assert evidence_checks.validate_results(results, set(wanted)) == []


def test_stepped_scenario_reports_sdk_size_and_every_step(tmp_path):
    # The size column is "envelope size as the SDK measures it": for a
    # stepped scenario it must be the largest single call's raw stdout,
    # never the size of the multi-step rendering. The command column must
    # name every step that ran, in order.
    require_test_tool(openssl is not None, "openssl is not installed")
    scenarios, volatile_keys = pr_evidence.load_scenarios(ROOT)
    stepped = next(
        scenario for scenario in scenarios if scenario["id"] == "unifi-wifi-get-saved"
    )
    volatile = set(volatile_keys)
    spec = pr_evidence.validate_fake_spec(stepped["fake"], stepped["id"])
    raw_sizes = []
    real_run_one_step = pr_evidence._run_one_step

    def recording(*args, **kwargs):
        result = real_run_one_step(*args, **kwargs)
        raw_sizes.append(len(json.dumps(result["stdout"])) - 2)
        return result

    certdir = tempfile.mkdtemp(prefix="hs-evidence-test-cert-", dir=str(tmp_path))
    try:
        cert_paths = pr_evidence.make_cert(certdir)
        with pr_evidence.run_fake(ROOT, spec, *cert_paths) as fake:
            pr_evidence._run_one_step = recording
            try:
                result = pr_evidence.run_scenario(ROOT, stepped, fake, volatile)
            finally:
                pr_evidence._run_one_step = real_run_one_step
    except pr_evidence._SkipScenario as exc:
        require_test_tool(False, str(exc))
        return
    assert len(raw_sizes) == 2
    assert result["sdk_size"] == max(raw_sizes)
    assert result["sdk_size"] < len(json.dumps(result["stdout"])) - 2
    doc = pr_evidence._results_from_runs(
        [stepped], volatile, [stepped], True, [(stepped, result, result)], None
    )
    (entry,) = doc["scenarios"]
    assert entry["size_after"] == max(raw_sizes)
    expected = []
    for index, step in enumerate(stepped["steps"]):
        if index:
            expected.append("&&")
        expected.extend(["housesitter", *step["argv"]])
    assert entry["command"] == expected
    assert evidence_checks.validate_results(doc, {stepped["id"]}) == []
