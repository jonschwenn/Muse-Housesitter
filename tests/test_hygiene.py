"""Repo hygiene scan (AGENTS.md, Testing rules).

Fails if any file git would include (tracked, or new and not ignored) contains
data that looks like it came from a real house, or a likely secret. Fixtures
must use invented values:

- MACs: 02:00:00:xx:xx:xx (locally administered) or 00:00:5e:00:53:xx (RFC 7042)
- IPv4: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 (RFC 5737), plus
  loopback and 0.0.0.0
- SSIDs in fixtures: end in "-example"
- Planted secrets in hostile fixtures: contain "PLANTED-SECRET"

The rules themselves live in scripts/hygiene_scan.py: the same code the
ci-guard job runs over the PR head tree's blobs, so a PR cannot weaken what
is enforced here by changing this file. The names below are aliases to that
module; the pinning tests read unchanged.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

SPEC = importlib.util.spec_from_file_location(
    "hygiene_scan", ROOT / "scripts" / "hygiene_scan.py"
)
hygiene_scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hygiene_scan)

# Aliases to the single source of truth in scripts/hygiene_scan.py.
mac_problems = hygiene_scan.mac_problems
ip_problems = hygiene_scan.ip_problems
secret_problems = hygiene_scan.secret_problems
ssid_problems = hygiene_scan.ssid_problems
SUPPRESS = hygiene_scan.SUPPRESS
PLANTED = hygiene_scan.PLANTED


def repo_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    files = []
    for name in out.decode().split("\0"):
        path = ROOT / name
        if name and path.is_file():
            files.append(path)
    return files


def scan(path: Path) -> list[str]:
    """Scan one working-tree file; outside the repo (tmp files) its name stands in."""
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.name
    return hygiene_scan.scan_bytes(rel, path.read_bytes())


def test_repo_has_no_real_house_data_or_secrets():
    report = []
    for path in repo_files():
        for problem in scan(path):
            report.append(f"{path.relative_to(ROOT)}: {problem}")
    assert not report, "Hygiene scan failed:\n" + "\n".join(report)


# The scanner itself must catch what it claims to. Bad values are built at
# run time so they never appear in the repo.

REAL_MAC = ["a4", "83", "e7", "12", "34", "56"]
REAL_IP = ["192", "168", "1", "10"]
PW = "pass" + "word"


@pytest.mark.parametrize(
    "text",
    [
        ":".join(REAL_MAC),
        "-".join(REAL_MAC).upper(),
        ".".join(["".join(REAL_MAC[0:2]), "".join(REAL_MAC[2:4]), "".join(REAL_MAC[4:6])]),
        "ap-" + "-".join(REAL_MAC),
        ":".join(REAL_MAC) + "-guest",
    ],
)
def test_scanner_flags_real_looking_macs(text):
    assert mac_problems(text)


def test_bare_macs_are_flagged_only_when_asked():
    assert mac_problems("".join(REAL_MAC), bare=True)
    assert not mac_problems("".join(REAL_MAC))
    assert not mac_problems("020000000001", bare=True)


@pytest.mark.parametrize(
    "text",
    [
        "02:00:00:00:00:01",
        "00:00:5E:00:53:2a",
        "0200.0000.0001",
        "AB:CD:EF",
        "3d3c42e5aac5ba805825da76410c181273ba90b1",  # a 40-hex git SHA
        "123e4567-e89b-12d3-a456-426614174000",  # a UUID
        '"rx_bytes": 123456789012',
    ],
)
def test_scanner_allows_invented_macs_and_hashes(text):
    assert not mac_problems(text, bare=True)
    assert not mac_problems(text)


@pytest.mark.parametrize(
    "text",
    [
        ".".join(REAL_IP),
        "at " + ".".join(REAL_IP) + ".",
        "host_" + ".".join(["10", "0", "0", "5"]),
        ".".join(["192", "168", "001", "010"]),
        "https://" + ".".join(REAL_IP) + ":8443/",
        "fe80::" + "a6:83ff:fee7:1234",
        ":".join(["2601", "1c0", "4401", "aa", "", "1"]),
    ],
)
def test_scanner_flags_real_ips(text):
    assert ip_problems(text)


@pytest.mark.parametrize(
    "text",
    [
        "192.0.2.1",
        "198.51.100.7",
        "127.0.0.1",
        "version 0.1.0.dev0",
        "firmware 6.6.77.15402",
        "2001:db8::1",
        "x[::2] and std::vector and 12:30:45",
        "ff02::1",
        '"netmask": "255.255.255.0"',
        "::ffff:192.0.2.1",
    ],
)
def test_scanner_allows_documentation_ips_and_lookalikes(text):
    assert not ip_problems(text)


@pytest.mark.parametrize(
    "text",
    [
        '"' + PW + '": "hunter2hunter2"',
        PW + ": hunter2hunter2",  # YAML
        "UNIFI_" + PW.upper() + "=hunter2hunter2",  # env file
        PW + " = hunter2hunter2",  # INI
        '"client_sec' + 'ret": "abcdefghij12"',
        '"access_tok' + 'en": "abcdefghij12"',
        "pass" + "wd: hunter2hunter2",
        '"api' + '-key": "abcdefghij12"',
        '"' + PW + '": "correct horse battery staple"',
        "https://admin:" + "hunter2hunter2@192.0.2.1/",
        "mg" + "st_" + "A" * 24,
        "export GH_TOK" + "EN=abcdefghij12",
        '"x_vwire' + 'key": "0123456789abcdef"',
        '"x_iapp_' + 'key": "0123456789abcdef"',
        '"cookie": "unifi' + 'ses=8f2a9c1d77e04b11; csrf_tok' + 'en=8f2a9c1d77e04b11"',
        "X-CSRF-Tok" + "en: 6f1e2d3c4b5a69788796a5b4",
        "Authorization: Bear" + "er abcdef0123456789abcdef",
        "ey" + "JhbGciOiJIUzI1NiJ9.ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0.sig",
    ],
)
def test_scanner_flags_secrets(text):
    assert secret_problems(text)


@pytest.mark.parametrize(
    "text",
    [
        '"' + PW + '": "' + PLANTED + '-1"',
        '"' + PW + '_sec' + 'ret": "unifi_' + PW + '"',  # names a secret file
        '"sec' + 'ret": "unifi_' + PW + '"',
        "GH_TOK" + "EN: ${{ github.token }}",
        "tok" + "en: $GITHUB_TOKEN",
        '"tok' + 'en_file": "<path to token file>"',
        "UNIFI_" + PW.upper() + "=changeme",  # .env.example placeholder
        "compass_heading: northwest",
        "tokenizer = Tokenizer()",
        "secret_id: abc12345678",
        "passive_scan: enabled",
        'CSRF_HEADER = "X-CSRF-Tok' + 'en"',
        'CSRF_TOK' + 'EN = "x-csrf-token"',
        'DEFAULT_SEC' + 'RET = "~/.config/housesitter/secrets/unifi"',
        '"x_auth' + 'key": "' + PLANTED + '-device-1"',
        "sort_key: name_and_time",
        "headers[" + '"X-CSRF-Tok' + 'en"] = csrf_token',
    ],
)
def test_scanner_allows_planted_and_referenced_secrets(text):
    assert not secret_problems(text)


@pytest.mark.parametrize(
    "line",
    [
        '"' + PW + '": ' + PW + ",",
        "body = {'username': username, '" + PW + "': " + PW + "}",
        PW + ' = ctx.secret("unifi_' + PW + '")',
        "tok" + "en = get_tok" + "en(path)",
        "self.tok" + "en = response.json()",
        "tok" + "en: Optional[str] = None",
        "csrf_tok" + 'en = resp.headers.get("X-CSRF-Token")',
    ],
)
def test_python_code_is_not_a_secret(line):
    assert not secret_problems(line, unquoted=False)


def test_python_string_literals_are_still_checked():
    assert secret_problems(PW + ' = "' + "hunter2hunter2" + '"', unquoted=False)


def test_suppression_marker_skips_only_its_line(tmp_path):
    flagged = tmp_path / "x.md"
    flagged.write_text(
        "a " + ".".join(REAL_IP) + "  <!-- " + SUPPRESS + " -->\n"
        "b " + ".".join(REAL_IP) + "\n"
    )
    assert scan(flagged) == [f"IPv4 {'.'.join(REAL_IP)}"]


def test_scanner_flags_real_ssids():
    assert ssid_problems({"clients": [{"essid": "MyHomeWifi"}]})
    assert not ssid_problems({"clients": [{"essid": "main-example"}]})


# Trusted scan: the ci-guard job runs the base copy of scripts/hygiene_scan.py
# over the PR head tree's blobs (never checked out, never imported). These
# tests pin that path: each builds a throwaway git repo whose head commit
# carries one of the three bypasses that silence the pytest-run scan (a pytest
# config that deselects this test, a tests/core/helpers.py patch, a
# src/housesitter/__init__.py patch) plus planted real-looking data, then runs
# the scanner the way the workflow does — ``python -I`` on the base file over
# git objects, with no pytest config or repo code involved — and shows the
# planted data is still flagged. Values are built at run time so they never
# appear in this repo.

REAL10 = ["10", "1", "2", "3"]

NEEDS_GIT = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    run("add", "-A")
    run(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "test",
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def _scan_tree(repo, sha):
    """Run the scanner the way the workflow does: ``python -I`` over git objects."""
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "scripts" / "hygiene_scan.py"), "--git-tree", sha],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )


@NEEDS_GIT
def test_trusted_scan_flags_data_despite_pytest_config_bypass(tmp_path):
    """Bypass (a): a .pytest.ini that deselects the hygiene test is inert here."""
    planted = ".".join(REAL10)
    repo, sha = _git_repo(
        tmp_path,
        {
            ".pytest.ini": "[pytest]\naddopts = --deselect tests/test_hygiene.py\n",
            "notes.txt": f"server at {planted}\n",
        },
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1
    assert f"IPv4 {planted}" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_flags_data_despite_helpers_patch_bypass(tmp_path):
    """Bypass (b): patching tests/core/helpers.py cannot neuter the blob scan."""
    planted = ".".join(REAL10)
    repo, sha = _git_repo(
        tmp_path,
        {
            "tests/core/helpers.py": "import test_hygiene\ntest_hygiene.repo_files = lambda: []\n",
            "notes.txt": f"server at {planted}\n",
        },
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1
    assert f"IPv4 {planted}" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_flags_data_despite_package_init_patch_bypass(tmp_path):
    """Bypass (c): patching src/housesitter/__init__.py cannot neuter the blob scan."""
    planted = ".".join(REAL10)
    repo, sha = _git_repo(
        tmp_path,
        {
            "src/housesitter/__init__.py": (
                "import test_hygiene\ntest_hygiene.repo_files = lambda: []\n"
            ),
            "notes.txt": f"server at {planted}\n",
        },
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1
    assert f"IPv4 {planted}" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_passes_a_clean_tree(tmp_path):
    repo, sha = _git_repo(tmp_path, {"README.md": "docs range 192.0.2.1\n"})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 0
    assert "clean" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_lists_suppressions(tmp_path):
    planted = ".".join(REAL10)
    repo, sha = _git_repo(
        tmp_path, {"notes.txt": "at " + planted + "  <!-- " + SUPPRESS + " -->\n"}
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 0
    assert "suppression notes.txt:1:" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_fails_closed_on_a_bad_sha(tmp_path):
    repo, _ = _git_repo(tmp_path, {"README.md": "hello\n"})
    proc = _scan_tree(repo, "0" * 40)
    assert proc.returncode == 2
    assert "fail closed" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_fails_closed_on_an_oversized_blob(tmp_path):
    repo, sha = _git_repo(tmp_path, {"big.bin": "x" * (hygiene_scan.MAX_FILE_BYTES + 1)})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 2
    assert "limit" in proc.stdout


def _origin_with_pr_head(tmp_path, files, number="7"):
    """A local stand-in for GitHub: an origin repo plus a clone of it.

    Creates ``refs/pull/<number>/head`` in the origin (that ref is what the
    workflow fetches) and returns ``(work_clone, head_sha)``.
    """
    origin, sha = _git_repo(tmp_path / "origin", files)
    subprocess.run(
        ["git", "update-ref", f"refs/pull/{number}/head", sha],
        cwd=origin,
        check=True,
        capture_output=True,
    )
    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True
    )
    return work, sha


@NEEDS_GIT
def test_fetch_verifies_the_head_sha_before_scanning(tmp_path, monkeypatch):
    planted = ".".join(REAL10)
    work, sha = _origin_with_pr_head(
        tmp_path, {"notes.txt": f"server at {planted}\n", "README.md": "docs\n"}
    )
    monkeypatch.setenv("PR_NUMBER", "7")
    monkeypatch.setenv("HEAD_SHA", sha)
    assert hygiene_scan.fetch_pr_head(cwd=str(work)) == sha
    # A moved PR ref must not be scanned: the SHAs no longer match.
    monkeypatch.setenv("HEAD_SHA", "f" * 40)
    try:
        hygiene_scan.fetch_pr_head(cwd=str(work))
    except hygiene_scan.FailClosed as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected FailClosed on a moved head ref")
    # Garbage environment fails closed without touching the network.
    monkeypatch.setenv("PR_NUMBER", "not-a-number")
    try:
        hygiene_scan.fetch_pr_head(cwd=str(work))
    except hygiene_scan.FailClosed as exc:
        assert "PR_NUMBER" in str(exc)
    else:
        raise AssertionError("expected FailClosed on a bad PR_NUMBER")


@NEEDS_GIT
def test_fetch_and_scan_end_to_end_like_the_workflow(tmp_path):
    """The workflow's exact command against a local origin with a planted IP."""
    planted = ".".join(REAL10)
    work, sha = _origin_with_pr_head(tmp_path, {"notes.txt": f"server at {planted}\n"})
    env = dict(os.environ, PR_NUMBER="7", HEAD_SHA=sha)
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "hygiene_scan.py"),
            "--fetch-and-scan-pr-head",
        ],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1
    assert f"IPv4 {planted}" in proc.stdout
