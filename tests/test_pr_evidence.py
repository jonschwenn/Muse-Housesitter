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
