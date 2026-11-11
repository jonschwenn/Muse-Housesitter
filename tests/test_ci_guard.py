"""Tests for scripts/ci_guard.py. No network: the API is faked."""

from __future__ import annotations

import ast
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
        "scripts/test_results.py",
        "scripts/evidence_checks.py",
        "tests/test_evidence_checks.py",
        "tests/test_test_results.py",
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
        ".gitattributes",
        "src/.gitattributes",
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


# CLI wiring: main() ties listing, lookup, and decision to exit codes ----------

# Planted so the hygiene scan allows it and a leak is easy to spot.
PLANTED_TOKEN = "PLANTED-SECRET-ci-guard-token"
# The annotation prefix main() prints on a pass. Kept as one constant so the
# text after it never sits next to it in source (the hygiene scan would read
# "...d::c..." as an IPv6 address).
NOTICE = "::notice title=ci-guard::"


class FakeApi:
    """Stands in for ``api_get``: serves canned file pages, author, permission."""

    def __init__(self, pages, *, author="octo", permission="write", files_error=None,
                 permission_error=None):
        self.pages = pages
        self.author = author
        self.permission = permission
        self.files_error = files_error
        self.permission_error = permission_error
        self.calls = []

    def __call__(self, repo, pr, token, subpath):
        assert (repo, pr, token) == ("o/r", 7, PLANTED_TOKEN)
        self.calls.append(subpath)
        if subpath.startswith("files?"):
            if self.files_error is not None:
                raise self.files_error
            page = int(subpath.rsplit("page=", 1)[1])
            return self.pages[page - 1] if page <= len(self.pages) else []
        if subpath == "":
            return {"user": {"login": self.author}}
        if subpath == f"collaborators/{self.author}/permission":
            if self.permission_error is not None:
                raise self.permission_error
            return {"permission": self.permission}
        raise AssertionError(f"unexpected API path: {subpath!r}")


def _run_main(monkeypatch, capsys, fake):
    monkeypatch.setenv("GH_TOKEN", PLANTED_TOKEN)
    monkeypatch.setattr(ci_guard, "api_get", fake)
    code = ci_guard.main(["--repo", "o/r", "--pr", "7"])
    out, err = capsys.readouterr()
    assert PLANTED_TOKEN not in out
    assert PLANTED_TOKEN not in err
    assert "PLANTED-SECRET" not in out + err
    return code, out, err


def _files(*names):
    return [[{"filename": name} for name in names]]


def test_main_no_protected_paths_passes_without_lookups(monkeypatch, capsys):
    fake = FakeApi(_files("docs/GITHUB.md", "web/index.html"))
    code, out, err = _run_main(monkeypatch, capsys, fake)
    assert code == 0
    assert out == "ci_guard: ok: no protected paths among 2 changed file(s)\n"
    assert err == ""
    # Only the file listing was fetched: no author or permission lookup.
    assert fake.calls == ["files?per_page=100&page=1"]


# GitHub's legacy `permission` field reports maintain as write and triage as
# read; the extra values still exercise the decision table.
@pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
def test_main_trusted_author_passes_with_notice(monkeypatch, capsys, permission):
    fake = FakeApi(
        _files(".github/workflows/ci.yml", "pyproject.toml", "docs/GITHUB.md"),
        permission=permission,
    )
    code, out, err = _run_main(monkeypatch, capsys, fake)
    assert code == 0
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(NOTICE + "ci_guard: ok: trusted author")
    assert ".github/workflows/ci.yml, pyproject.toml" in lines[0]
    assert "docs/GITHUB.md" not in lines[0]
    assert f"(author @octo has '{permission}')" in lines[0]
    assert fake.calls[1:] == ["", "collaborators/octo/permission"]


@pytest.mark.parametrize(
    "permission, permission_error",
    [
        ("read", None),
        ("triage", None),
        (None, ci_guard.ApiError("GET .../permission failed with HTTP 404")),
    ],
)
def test_main_untrusted_author_is_blocked(monkeypatch, capsys, permission, permission_error):
    fake = FakeApi(
        _files(".github/workflows/ci.yml", "tests/conftest.py", "README.md"),
        author="stranger",
        permission=permission,
        permission_error=permission_error,
    )
    code, out, err = _run_main(monkeypatch, capsys, fake)
    expected = permission or "none"
    assert code == 1
    lines = out.splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("::error::ci_guard: blocked: PR touches protected path(s): ")
    for path in (".github/workflows/ci.yml", "tests/conftest.py"):
        assert f"{path} ({ci_guard.is_protected(path)})" in line
    assert "README.md" not in line
    assert f"(author @stranger has '{expected}'; needs admin, maintain, or write)" in line
    assert "D16" in err


def test_main_listing_error_fails_closed(monkeypatch, capsys):
    fake = FakeApi(
        [], files_error=ci_guard.ApiError("GET https://example.invalid/x failed with HTTP 500")
    )
    code, out, _ = _run_main(monkeypatch, capsys, fake)
    assert code == 2
    assert out.startswith("::error::ci_guard: fail closed: ")
    assert "HTTP 500" in out
    assert len(out.splitlines()) == 1
    assert fake.calls == ["files?per_page=100&page=1"]


def test_main_listing_unexpected_exception_fails_closed(monkeypatch, capsys):
    fake = FakeApi([], files_error=RuntimeError("boom"))
    code, out, _ = _run_main(monkeypatch, capsys, fake)
    assert code == 2
    assert out.startswith("::error::ci_guard: fail closed: listing page 1 failed: boom")


@pytest.mark.parametrize("status", ["500", "403"])
def test_main_permission_error_fails_closed(monkeypatch, capsys, status):
    fake = FakeApi(
        _files("pyproject.toml"),
        permission_error=ci_guard.ApiError(f"GET .../permission failed with HTTP {status}"),
    )
    code, out, _ = _run_main(monkeypatch, capsys, fake)
    assert code == 2
    assert out.startswith("::error::ci_guard: fail closed: ")
    assert f"HTTP {status}" in out


def test_main_bad_author_body_fails_closed(monkeypatch, capsys):
    fake = FakeApi(_files("pyproject.toml"))

    def no_author(repo, pr, token, subpath):
        if subpath == "":
            return {"user": None}
        return fake(repo, pr, token, subpath)

    code, out, _ = _run_main(monkeypatch, capsys, no_author)
    assert code == 2
    assert out.startswith("::error::ci_guard: fail closed: PR lookup returned no author login")


def test_main_thirty_full_pages_fail_closed(monkeypatch, capsys):
    full = [{"filename": f"docs/f{i}.md"} for i in range(100)]
    fake = FakeApi([full] * 30)
    code, out, _ = _run_main(monkeypatch, capsys, fake)
    assert code == 2
    assert out == (
        "::error::ci_guard: fail closed: PR touches 3000 or more files; "
        "GitHub caps the listing, so the change cannot be checked\n"
    )
    assert len(fake.calls) == 30
    assert "" not in fake.calls  # never got as far as the author lookup


@pytest.mark.parametrize("permission, expected_code", [("write", 0), ("read", 1)])
def test_main_newline_in_file_name_cannot_inject_annotations(
    monkeypatch, capsys, permission, expected_code
):
    evil = ".github/x\n" + NOTICE + "ci_guard: ok\r%0A.yml"
    fake = FakeApi(_files(evil), permission=permission)
    code, out, _ = _run_main(monkeypatch, capsys, fake)
    assert code == expected_code
    lines = out.splitlines()
    assert len(lines) == 1, lines
    # The injected text stays inside the one real annotation, escaped.
    assert lines[0].startswith(NOTICE if expected_code == 0 else "::error::")
    assert ".github/x%0A" + NOTICE + "ci_guard: ok%0D%250A.yml" in lines[0]


# Review round 1, finding 2: the validator's own protection -------------------
#
# scripts/evidence_checks.py shapes what the privileged publisher accepts,
# and tests/test_evidence_checks.py could mask a weakened copy, so both are
# protected paths and the test file re-runs in CI's isolated protected
# suites. The import test below keeps this closed as scripts gain helpers:
# anything a protected script imports is itself protected.


def test_local_imports_of_protected_scripts_are_protected():
    scripts = ROOT / "scripts"
    protected_scripts = sorted(
        path
        for path in ci_guard.EXACT_PROTECTED
        if path.startswith("scripts/") and path.endswith(".py")
    )
    assert protected_scripts, "no protected scripts to check"
    for rel in protected_scripts:
        tree = ast.parse((ROOT / rel).read_text(), filename=rel)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for name in sorted(imported):
            if (scripts / f"{name}.py").is_file():
                assert ci_guard.is_protected(f"scripts/{name}.py") is not None, (
                    f"{rel} imports scripts/{name}.py, which is not protected"
                )


def test_protected_test_files_rerun_in_isolated_suites():
    # ci.yml re-runs the protected suites in a pytest process that never
    # imports an unprotected test module (D16). Every protected test file
    # must be named in that isolated run, or its pinning is decorative.
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    protected_tests = sorted(
        path
        for path in ci_guard.EXACT_PROTECTED
        if path.startswith("tests/test_") and path.endswith(".py")
    )
    assert protected_tests, "no protected test files to check"
    for path in protected_tests:
        assert path in workflow, f"{path} is protected but missing from ci.yml"
