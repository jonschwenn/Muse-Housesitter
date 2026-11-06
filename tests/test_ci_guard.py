"""Tests for scripts/ci_guard.py. No network: the API is faked."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ci_guard", ROOT / "scripts" / "ci_guard.py")
ci_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_guard)


# Protected-path matching -----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        ".github/workflows/evidence-publish.yml",
        ".github/workflows/ci-guard.yml",
        ".github/rulesets/protect-main.json",
        ".github/pull_request_template.md",
        "tests/test_hygiene.py",
        "tests/test_pr_evidence.py",
        "tests/test_ui_evidence.py",
        "tests/test_publish_evidence.py",
        "tests/test_ci_guard.py",
        "tests/conformance/test_example.py",
        "tests/evidence/scenarios.json",
        "scripts/pr_evidence.py",
        "scripts/ui_evidence.py",
        "scripts/publish_evidence.py",
        "scripts/ci_guard.py",
        "pyproject.toml",
        "conftest.py",
        "tests/conftest.py",
        "tests/modules/conftest.py",
        "pytest.ini",
        "tests/pytest.ini",
        "setup.cfg",
        "tox.ini",
        "ruff.toml",
        ".ruff.toml",
        "sub/ruff.toml",
        "sitecustomize.py",
        "usercustomize.py",
        "evil.pth",
        "tests/evil.pth",
        "tests/__init__.py",
        "tests/modules/__init__.py",
    ],
)
def test_protected_paths_are_caught(path):
    assert ci_guard.is_protected(path) is not None, path


@pytest.mark.parametrize(
    "path",
    [
        "src/housesitter/modules/unifi_wifi/__init__.py",
        "src/housesitter/core/http.py",
        "tests/test_cli.py",
        "tests/modules/test_something.py",
        "tests/test_brand_new.py",
        "docs/GITHUB.md",
        "README.md",
        "web/index.html",
        "scripts/dev_dashboard.py",
    ],
)
def test_ordinary_paths_pass(path):
    assert ci_guard.is_protected(path) is None, path


def test_reason_names_what_is_protected():
    assert "pytest" in (ci_guard.is_protected("conftest.py") or "")
    assert "start-up" in (ci_guard.is_protected("x.pth") or "")


# File listing ----------------------------------------------------------------


def _pages(*pages):
    calls = []

    def get(subpath):
        calls.append(subpath)
        assert subpath.startswith("files?")
        page = int(subpath.rsplit("page=", 1)[1])
        return pages[page - 1]

    return get, calls


def test_single_short_page_lists_filenames():
    get, _ = _pages([{"filename": "a.py"}, {"filename": "b/c.py"}])
    assert ci_guard.list_changed_files(get) == ["a.py", "b/c.py"]


def test_renames_include_both_names():
    get, _ = _pages([{"filename": "new.py", "previous_filename": "old.py"}])
    assert ci_guard.list_changed_files(get) == ["new.py", "old.py"]


def test_pagination_continues_past_full_pages():
    full = [{"filename": f"f{i}.py"} for i in range(100)]
    get, calls = _pages(full, [{"filename": "last.py"}])
    assert ci_guard.list_changed_files(get) == [e["filename"] for e in full] + ["last.py"]
    assert len(calls) == 2


def test_api_error_fails_closed():
    def get(subpath):
        raise ci_guard.ApiError("GET https://example.invalid/x failed with HTTP 500")

    with pytest.raises(ci_guard.ApiError):
        ci_guard.list_changed_files(get)


def test_unexpected_body_fails_closed():
    get, _ = _pages({"files": []})
    with pytest.raises(ci_guard.ApiError):
        ci_guard.list_changed_files(get)


def test_thirty_full_pages_fail_closed():
    full = [{"filename": f"f{i}.py"} for i in range(100)]
    get, _ = _pages(*([full] * 30))
    with pytest.raises(ci_guard.TooManyFiles):
        ci_guard.list_changed_files(get)


def test_pr_author_comes_from_the_pr_object():
    assert ci_guard.get_pr_author(lambda sub: {"user": {"login": "octo"}}) == "octo"


def test_missing_author_fails_closed():
    with pytest.raises(ci_guard.ApiError):
        ci_guard.get_pr_author(lambda sub: {"user": {}})


def test_unknown_collaborator_counts_as_none():
    def get(subpath):
        raise ci_guard.ApiError("GET .../permission failed with HTTP 404")

    assert ci_guard.get_permission(get, "stranger") == "none"


def test_other_permission_errors_fail_closed():
    def get(subpath):
        raise ci_guard.ApiError("GET .../permission failed with HTTP 500")

    with pytest.raises(ci_guard.ApiError):
        ci_guard.get_permission(get, "octo")


# Decision --------------------------------------------------------------------


def test_no_protected_paths_passes_without_a_permission():
    ok, protected, message = ci_guard.evaluate(["docs/GITHUB.md", "web/a.html"], None)
    assert ok and protected == []
    assert "no protected paths" in message


def test_trusted_permission_passes_with_protected_paths():
    for permission in ("admin", "maintain", "write"):
        ok, protected, _ = ci_guard.evaluate(["pyproject.toml"], permission)
        assert ok and protected == ["pyproject.toml"], permission


@pytest.mark.parametrize("permission", ["read", "none", None])
def test_untrusted_permission_blocks(permission):
    ok, protected, message = ci_guard.evaluate(
        ["pyproject.toml", "docs/GITHUB.md"], permission
    )
    assert not ok
    assert protected == ["pyproject.toml"]
    assert "pyproject.toml" in message


def test_blocked_message_names_every_protected_file():
    ok, _, message = ci_guard.evaluate(
        [".github/workflows/ci.yml", "pyproject.toml"], "read"
    )
    assert not ok
    assert ".github/workflows/ci.yml" in message
    assert "pyproject.toml" in message


# Annotation escaping ----------------------------------------------------------


def test_annotation_escaping_neutralises_newlines():
    assert ci_guard.escape_data("a\nb\rc%d") == "a%0Ab%0Dc%25d"


# CLI errors ------------------------------------------------------------------


def test_missing_token_fails_closed(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert ci_guard.main(["--repo", "o/r", "--pr", "1"]) == 2
    assert "GH_TOKEN" in capsys.readouterr().err


def test_bad_pr_number_fails_closed(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    assert ci_guard.main(["--repo", "o/r", "--pr", "0"]) == 2
