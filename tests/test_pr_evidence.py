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
