#!/usr/bin/env python3
"""Block untrusted changes to the testing/validation mechanism (D16).

Lists a pull request's changed files through the GitHub API and refuses to
pass when the PR touches a protected path unless its author has admin,
maintain, or write permission on the repository. Runs on
``pull_request_target`` from the base commit, so it never checks out or
executes PR code; the token is used for read-only API calls only.

Usage:
    GH_TOKEN=... python scripts/ci_guard.py --repo OWNER/NAME --pr 12

Exit codes: 0 passes, 1 blocks (protected paths touched by an author without
write access), 2 fails closed (the file list or the permission could not be
established: API error, or the PR may exceed GitHub's 3000-file listing cap).

The token comes from the ``GH_TOKEN`` environment variable only, never from
argv (AGENTS.md rule 6: secrets never leave their file or appear in argv).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Files whose change alters what CI validates, how it validates, or what the
# privileged publisher runs. Each entry: (kind, pattern, why).
EXACT_PROTECTED = {
    # Evidence builders, validators, and renderers: PR code that produces
    # the published comment and screenshots, and main's code that the
    # privileged publisher imports to validate and render them.
    "scripts/pr_evidence.py": "builds the evidence comment",
    "scripts/ui_evidence.py": "captures screenshots and validates the artifact",
    "scripts/publish_evidence.py": "validates and renders evidence with write tokens",
    "scripts/test_results.py": "parses test XML and renders the Tests line",
    "scripts/evidence_checks.py": "shared refusal checks the publisher imports",
    "tests/test_evidence_checks.py": "pins the shared refusal checks",
    # The guard itself and the tests that pin all of the above: editing the
    # tests could mask a weakened script, so they are protected too.
    "scripts/ci_guard.py": "this guard",
    "tests/test_hygiene.py": "pins the repo hygiene scan",
    "tests/test_pr_evidence.py": "pins the evidence comment builder",
    "tests/test_ui_evidence.py": "pins the screenshot capture and validator",
    "tests/test_publish_evidence.py": "pins the privileged publisher",
    "tests/test_test_results.py": "pins the test-results parser and renderers",
    "tests/test_ci_guard.py": "pins this guard",
    # Build and test configuration: pytest/ruff settings (testpaths, addopts,
    # deselects, ignores), dependency pins, and packaging entry points all
    # live here. A weaker pytest or ruff config is a weaker CI.
    "pyproject.toml": "pytest/ruff config, dev dependency pins, entry points",
}

PREFIX_PROTECTED = {
    # Workflows, the ruleset file, and templates: what CI runs, what it
    # requires, and what reviewers see.
    ".github/": "workflows and branch protection",
    # The module conformance suite: every module must pass it.
    "tests/conformance/": "the module conformance suite",
    # The evidence scenarios: what the evidence covers. Removing a scenario
    # hides a regression the same way editing the builder does.
    "tests/evidence/": "the evidence scenario list",
}

# File names that are dangerous anywhere in the tree: pytest/ruff/tooling
# config (a stray pytest.ini overrides pyproject.toml), interpreter
# start-up hooks (sitecustomize/usercustomize/*.pth run code in every
# python process, including CI's), and conftest.py (fixtures, hooks, and
# pytest_plugins that apply to the whole directory below it).
NAME_PROTECTED = {
    "conftest.py": "pytest fixtures, hooks, and plugins for its directory",
    "pytest.ini": "overrides the pytest config in pyproject.toml",
    "setup.cfg": "may carry pytest or tool config",
    "tox.ini": "may carry pytest or tool config",
    "ruff.toml": "overrides the ruff config in pyproject.toml",
    ".ruff.toml": "overrides the ruff config in pyproject.toml",
    "sitecustomize.py": "runs at every interpreter start-up",
    "usercustomize.py": "runs at every interpreter start-up",
    # linguist-generated / -diff collapse or hide a file's diff in PR review.
    ".gitattributes": "can hide file diffs from reviewers",
}

# An __init__.py under tests/ flips pytest's collection from rootless to
# package-based imports, changing how every test module is imported (and
# which module object a cross-import would patch). New modules under src/
# still need their own __init__.py files, so this rule is tests-only.
TESTS_INIT_REASON = "changes pytest's import mode for the whole suite"

TRUSTED_PERMISSIONS = ("admin", "maintain", "write")

# GitHub caps the pull-request files listing at 3000 files.
PER_PAGE = 100
MAX_PAGES = 30
MAX_FILES = PER_PAGE * MAX_PAGES

API_TIMEOUT_S = 30


class ApiError(Exception):
    pass


class TooManyFiles(ApiError):
    pass


def escape_data(text: str) -> str:
    """Escape a value for a GitHub workflow command (``::notice::``/``::error::``).

    File names come from the API and may contain anything, including
    newlines that would otherwise inject fake annotations into the log.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def is_protected(path: str) -> str | None:
    """Return why ``path`` (a repo-relative POSIX path) is protected, or None."""
    normal = path.strip()
    while normal.startswith("./"):
        normal = normal[2:]
    normal = normal.lstrip("/")
    if normal in EXACT_PROTECTED:
        return EXACT_PROTECTED[normal]
    for prefix, why in PREFIX_PROTECTED.items():
        if normal == prefix.rstrip("/") or normal.startswith(prefix):
            return why
    name = normal.rsplit("/", 1)[-1]
    if name in NAME_PROTECTED:
        return NAME_PROTECTED[name]
    if name.endswith(".pth"):
        return "runs at interpreter start-up"
    if name == "__init__.py" and (normal == "tests/__init__.py" or normal.startswith("tests/")):
        return TESTS_INIT_REASON
    return None


def list_changed_files(get) -> list[str]:
    """List every changed file via paginated API calls.

    ``get`` takes an API path and returns parsed JSON. Raises ApiError on
    any failure and TooManyFiles when the PR may exceed GitHub's 3000-file
    listing cap (fail closed: a truncated list must never pass).
    """
    changed: list[str] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            data = get(f"files?per_page={PER_PAGE}&page={page}")
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(f"listing page {page} failed: {exc}") from exc
        if not isinstance(data, list):
            raise ApiError(f"listing page {page} returned {type(data).__name__}")
        for entry in data:
            if not isinstance(entry, dict):
                raise ApiError(f"listing page {page} returned a bad entry")
            for key in ("filename", "previous_filename"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    changed.append(value)
        if len(data) < PER_PAGE:
            return changed
    # Every page was full: the list may be truncated at GitHub's cap.
    raise TooManyFiles(
        "PR touches 3000 or more files; GitHub caps the listing, "
        "so the change cannot be checked"
    )


def get_pr_author(get) -> str:
    data = get("")
    if not isinstance(data, dict):
        raise ApiError("PR lookup returned an unexpected body")
    user = data.get("user")
    if not isinstance(user, dict) or not isinstance(user.get("login"), str):
        raise ApiError("PR lookup returned no author login")
    return user["login"]


def get_permission(get, username: str) -> str:
    try:
        data = get(f"collaborators/{username}/permission")
    except ApiError as exc:
        # A non-collaborator has no permission entry at all.
        if "404" in str(exc):
            return "none"
        raise
    if not isinstance(data, dict) or not isinstance(data.get("permission"), str):
        raise ApiError("permission lookup returned an unexpected body")
    return data["permission"]


def evaluate(changed: list[str], permission: str | None) -> tuple[bool, list[str], str]:
    """Decide the check. Returns (ok, protected_files, message).

    ``permission`` is None when no protected path was touched (no lookup
    needed) or the lookup is skipped in tests.
    """
    protected = sorted({path for path in changed if is_protected(path) is not None})
    if not protected:
        return True, [], f"ci_guard: ok: no protected paths among {len(changed)} changed file(s)"
    if permission in TRUSTED_PERMISSIONS:
        files = ", ".join(protected)
        return True, protected, f"ci_guard: ok: trusted author touched protected path(s): {files}"
    return (
        False,
        protected,
        "ci_guard: blocked: PR touches protected path(s): "
        + ", ".join(f"{p} ({is_protected(p)})" for p in protected),
    )


def api_get(repo: str, pr: int, token: str, subpath: str):
    """GET a pull-request-scoped API path. Returns parsed JSON."""
    if subpath and not subpath.startswith(("files?", "collaborators/")):
        raise ApiError(f"refusing unexpected API path: {subpath!r}")
    base = f"https://api.github.com/repos/{repo}/pulls/{pr}/"
    if subpath.startswith("collaborators/"):
        url = f"https://api.github.com/repos/{repo}/{subpath}"
    elif subpath:
        url = base + subpath
    else:
        url = base.rstrip("/")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "muse-housesitter-ci-guard",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as exc:
        raise ApiError(f"GET {url} failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise ApiError(f"GET {url} failed: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    parser.add_argument("--pr", required=True, type=int, help="pull request number")
    args = parser.parse_args(argv)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        print("ci_guard: GH_TOKEN is not set", file=sys.stderr)
        return 2
    if args.pr <= 0:
        print(f"ci_guard: bad PR number: {args.pr}", file=sys.stderr)
        return 2

    def get(subpath: str):
        return api_get(args.repo, args.pr, token, subpath)

    try:
        changed = list_changed_files(get)
    except TooManyFiles as exc:
        print(f"::error::{escape_data(f'ci_guard: fail closed: {exc}')}")
        return 2
    except ApiError as exc:
        print(f"::error::{escape_data(f'ci_guard: fail closed: {exc}')}")
        return 2

    protected = sorted({p for p in changed if is_protected(p) is not None})
    if not protected:
        print(f"ci_guard: ok: no protected paths among {len(changed)} changed file(s)")
        return 0

    try:
        author = get_pr_author(get)
        permission = get_permission(get, author)
    except ApiError as exc:
        print(f"::error::{escape_data(f'ci_guard: fail closed: {exc}')}")
        return 2

    ok, _, message = evaluate(changed, permission)
    safe = escape_data(message)
    if ok:
        print(f"::notice title=ci-guard::{safe} (author @{author} has '{permission}')")
        return 0
    print(f"::error::{safe} (author @{author} has '{permission}'; needs admin, maintain, or write)")
    print(
        "A collaborator with write access must make this change instead; "
        "fork PRs cannot touch these paths (D16).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
