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
        "scripts/hygiene_scan.py",
        "scripts/test_results.py",
        "tests/test_test_results.py",
        "tests/core/helpers.py",
        "tests/fixtures/unifi_wifi/conformance.json",
        "tests/fixtures/other_mod/conformance.json",
        "pyproject.toml",
        "config/pyproject.toml",
        "setup.py",
        "pkg/setup.py",
        "conftest.py",
        "tests/conftest.py",
        "tests/modules/conftest.py",
        "pytest.ini",
        ".pytest.ini",
        "tests/pytest.ini",
        "tests/.pytest.ini",
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
        "tests/fixtures/unifi_wifi/classic_sta.json",
        "tests/fixtures/conformance.json",
        "tests/fixtures/a/b/conformance.json",
        "tests/conformance.json",
        "setup.cfg.bak",
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


# Import closure -------------------------------------------------------------
# Every module a protected suite imports from tests/ or loads from scripts/
# must itself be protected: otherwise a PR could weaken the suite through an
# unprotected file (D16). This walks the imports statically (ast), so it never
# executes the files it checks. Imports that resolve nowhere under tests/
# (stdlib, pytest, third-party, src/) are not the guard's business: src/ in
# particular cannot be protected, because the conformance suite necessarily
# executes PR module code.

PROTECTED_SUITE_FILES = [
    "tests/test_hygiene.py",
    "tests/test_pr_evidence.py",
    "tests/test_ui_evidence.py",
    "tests/test_publish_evidence.py",
    "tests/test_test_results.py",
    "tests/test_ci_guard.py",
]
PROTECTED_SUITE_DIRS = ["tests/conformance"]


def _suite_files():
    files = [ROOT / name for name in PROTECTED_SUITE_FILES]
    for dirname in PROTECTED_SUITE_DIRS:
        files.extend(sorted((ROOT / dirname).rglob("*.py")))
    return files


def _resolve_import(path, level, dotted, root):
    """Repo-relative paths under tests/ that an import statement may load."""
    parts = dotted.split(".") if dotted else []
    if level:
        base = path.parent
        for _ in range(level - 1):
            base = base.parent
        roots = [base]
    else:
        roots = [root / "tests", path.parent]
    found = []
    for search in roots:
        for candidate in (
            search.joinpath(*parts).with_suffix(".py"),
            search.joinpath(*parts, "__init__.py"),
        ):
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if candidate.is_file():
                found.append(rel)
    return found


def _spec_load_targets(tree):
    """Scripts loaded via ``spec_from_file_location``: repo-relative paths."""
    calls = []  # (enclosing function name or None, string constants in args)

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
            else:
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "spec_from_file_location"
                ):
                    consts = [
                        constant.value
                        for constant in ast.walk(child)
                        if isinstance(constant, ast.Constant)
                        and isinstance(constant.value, str)
                    ]
                    calls.append((func, consts))
                visit(child, func)

    visit(tree, None)
    first_args = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            first_args.setdefault(node.func.id, set()).add(node.args[0].value)
    targets = []
    for func, consts in calls:
        if "scripts" not in consts:
            continue
        pys = [c for c in consts if c.endswith(".py") and len(c) > len(".py")]
        if pys:
            targets.extend(f"scripts/{Path(p).name}" for p in pys)
        elif func in first_args:
            # Dynamic file name (``ROOT / "scripts" / f"{name}.py"``): resolve
            # through the loader's constant call sites, e.g. _load("ui_evidence").
            targets.extend(f"scripts/{name}.py" for name in first_args[func])
    return targets


def _from_import_targets(path, level, module, names, root):
    """What ``from X import a, b`` may load: X itself plus X/a, X/b.

    A ``from package import submodule`` line loads the submodule file too
    (``from core import evil`` loads ``tests/core/evil.py``), so every
    imported name is also tried as a submodule of the module.
    """
    targets = set(_resolve_import(path, level, module, root))
    base = module or ""
    for name in names:
        if name == "*":
            continue
        dotted = f"{base}.{name}" if base else name
        targets.update(_resolve_import(path, level, dotted, root))
    return targets


def _repo_local_targets(path, root):
    tree = ast.parse(path.read_text())
    targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import a.b`` loads a, a.b, and the leaf file alike.
                parts = alias.name.split(".")
                for depth in range(1, len(parts) + 1):
                    targets.update(
                        _resolve_import(path, 0, ".".join(parts[:depth]), root)
                    )
        elif isinstance(node, ast.ImportFrom):
            targets.update(
                _from_import_targets(
                    path, node.level, node.module or "", [a.name for a in node.names], root
                )
            )
    for target in _spec_load_targets(tree):
        candidate = root / target
        if candidate.is_file():
            targets.add(target)
    return sorted(targets)


def _closure_reachable(start, root):
    """Every file reachable from ``start`` through repo-local imports.

    Walks transitively: a protected helper importing an unprotected file
    weakens the suite the same way a direct import does, so the walk
    follows each reached file's own imports (tests/ and scripts/ only).
    """
    seen = set()
    queue = [start.relative_to(root).as_posix()]
    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        seen.add(rel)
        candidate = root / rel
        if candidate.suffix != ".py" or not candidate.is_file():
            continue
        for target in _repo_local_targets(candidate, root):
            if target not in seen:
                queue.append(target)
    seen.discard(start.relative_to(root).as_posix())
    return sorted(seen)


def test_every_repo_local_import_of_protected_suites_is_protected():
    unprotected = []
    for path in _suite_files():
        rel = path.relative_to(ROOT).as_posix()
        for target in _closure_reachable(path, ROOT):
            if ci_guard.is_protected(target) is None:
                unprotected.append(f"{rel} -> {target}")
    assert not unprotected, (
        "protected suites import unprotected files:\n" + "\n".join(unprotected)
    )


def _write_tree(root, files):
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)


def test_closure_follows_transitive_imports(tmp_path):
    """Finding 4: a protected helper importing an unprotected file is caught."""
    root = tmp_path / "r"
    _write_tree(
        root,
        {
            "tests/suite_a.py": "import helper_mod\n",
            "tests/helper_mod.py": "import evil_mod\n",
            "tests/evil_mod.py": "X = 1\n",
        },
    )
    reached = _closure_reachable(root / "tests" / "suite_a.py", root)
    assert "tests/evil_mod.py" in reached


def test_closure_resolves_from_import_submodule(tmp_path):
    """Finding 4: ``from core import evil`` resolves tests/core/evil.py."""
    root = tmp_path / "r"
    _write_tree(
        root,
        {
            "tests/suite_b.py": "from core import evil\n",
            "tests/core/evil.py": "X = 1\n",
        },
    )
    reached = _closure_reachable(root / "tests" / "suite_b.py", root)
    assert "tests/core/evil.py" in reached


def test_closure_resolves_relative_imports(tmp_path):
    """Finding 4: relative imports resolve against the importing file."""
    root = tmp_path / "r"
    _write_tree(
        root,
        {
            "tests/pkg/suite_c.py": "from . import sib\nfrom .. import top\n",
            "tests/pkg/sib.py": "X = 1\n",
            "tests/top.py": "X = 1\n",
        },
    )
    reached = _closure_reachable(root / "tests" / "pkg" / "suite_c.py", root)
    assert "tests/pkg/sib.py" in reached
    assert "tests/top.py" in reached


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


def test_head_repo_comes_from_the_pr_object():
    def get(sub):
        return {"head": {"repo": {"full_name": "fork-owner/repo"}}}

    assert ci_guard.get_head_repo_full_name(get) == "fork-owner/repo"


def test_null_head_repo_is_untrusted_not_an_error():
    def get(sub):
        return {"head": {"repo": None}}

    assert ci_guard.get_head_repo_full_name(get) is None


def test_missing_head_fails_closed():
    with pytest.raises(ci_guard.ApiError):
        ci_guard.get_head_repo_full_name(lambda sub: {"user": {"login": "octo"}})


def test_nameless_head_repo_fails_closed():
    with pytest.raises(ci_guard.ApiError):
        ci_guard.get_head_repo_full_name(lambda sub: {"head": {"repo": {}}})


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


def _same_repo(**kwargs):
    return {"head_repo_full_name": "o/r", "base_repo_full_name": "o/r", **kwargs}


def test_no_protected_paths_passes_without_a_permission():
    ok, protected, message = ci_guard.evaluate(
        ["docs/GITHUB.md", "web/a.html"], None, **_same_repo()
    )
    assert ok and protected == []
    assert "no protected paths" in message


def test_trusted_permission_passes_with_protected_paths():
    for permission in ("admin", "maintain", "write"):
        ok, protected, _ = ci_guard.evaluate(["pyproject.toml"], permission, **_same_repo())
        assert ok and protected == ["pyproject.toml"], permission


@pytest.mark.parametrize("permission", ["read", "none", None])
def test_untrusted_permission_blocks(permission):
    ok, protected, message = ci_guard.evaluate(
        ["pyproject.toml", "docs/GITHUB.md"], permission, **_same_repo()
    )
    assert not ok
    assert protected == ["pyproject.toml"]
    assert "pyproject.toml" in message


def test_fork_head_always_blocks_even_with_write_permission():
    ok, protected, message = ci_guard.evaluate(
        ["pyproject.toml"],
        "write",
        head_repo_full_name="fork-owner/repo",
        base_repo_full_name="o/r",
    )
    assert not ok
    assert protected == ["pyproject.toml"]
    assert "fork-owner/repo" in message
    assert "maintainer must recreate" in message


def test_null_head_repo_blocks():
    ok, _, message = ci_guard.evaluate(
        ["pyproject.toml"],
        "write",
        head_repo_full_name=None,
        base_repo_full_name="o/r",
    )
    assert not ok
    assert "unavailable" in message
    assert "maintainer must recreate" in message


def test_blocked_message_names_every_protected_file():
    ok, _, message = ci_guard.evaluate(
        [".github/workflows/ci.yml", "pyproject.toml"], "read", **_same_repo()
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

    def __init__(self, pages, *, author="octo", permission="write", head_repo="o/r",
                 files_error=None, permission_error=None):
        self.pages = pages
        self.author = author
        self.permission = permission
        # None stands for a deleted fork's null head repo.
        self.head_repo = head_repo
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
            head = (
                {"repo": {"full_name": self.head_repo}}
                if self.head_repo is not None
                else {"repo": None}
            )
            return {
                "user": {"login": self.author},
                "head": head,
                "base": {"repo": {"full_name": "o/r"}},
            }
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
    # The PR object is fetched once for the author and once for the head repo.
    assert fake.calls[1:] == ["", "", "collaborators/octo/permission"]


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


def test_main_fork_head_with_write_permission_is_blocked(monkeypatch, capsys):
    fake = FakeApi(
        _files(".github/workflows/ci.yml", "pyproject.toml"),
        author="octo",
        permission="write",
        head_repo="fork-owner/repo",
    )
    code, out, err = _run_main(monkeypatch, capsys, fake)
    assert code == 1
    lines = out.splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("::error::ci_guard: blocked: PR head repository is ")
    assert "fork-owner/repo" in line
    assert "maintainer must recreate" in line
    assert "(author @octo; head repo 'fork-owner/repo')" in line
    # Fork heads always fail: the permission lookup never runs.
    assert not any(call.startswith("collaborators/") for call in fake.calls)
    assert "D16" in err


def test_main_null_head_repo_is_blocked(monkeypatch, capsys):
    fake = FakeApi(
        _files("pyproject.toml"),
        author="octo",
        permission="write",
        head_repo=None,
    )
    code, out, err = _run_main(monkeypatch, capsys, fake)
    assert code == 1
    assert "unavailable" in out
    assert "maintainer must recreate" in out
    assert not any(call.startswith("collaborators/") for call in fake.calls)
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
