"""Tests for scripts/pr_evidence.py, run against throwaway source trees."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("pr_evidence", ROOT / "scripts" / "pr_evidence.py")
pr_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pr_evidence)


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
    scenarios, _ = pr_evidence.load_scenarios(ROOT)
    assert scenarios, "tests/evidence/scenarios.json must list at least one scenario"
    for scenario in scenarios:
        result = pr_evidence.run_scenario(ROOT, scenario)
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
    assert "<details><summary>typical</summary>" in full
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
