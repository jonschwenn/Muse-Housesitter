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


def _git_repo(tmp_path, files, links=None):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    for rel, text in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for rel, target_str in (links or {}).items():
        link = repo / rel
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target_str)
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
        timeout=300,
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
    # Only the path and line number are listed, never the line content.
    assert "suppression notes.txt:1\n" in proc.stdout


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


# Review round 1 (PR #56): the tree listing used ``--format=%(path)``,
# which C-quotes names holding a tab, newline, quote, or backslash even
# with ``-z``, so the quoted name missed the fixture prefix/.json suffix
# and fixture-only checks (bare MAC, SSID) were skipped. The scanner must
# read real path bytes, so every special character below is still flagged.
BYPASS_MAC = "".join(["a4", "83", "e7", "12", "34", "56"])
BYPASS_SSID = "MyHome" + "Wifi"


def _bypass_fixture_json():
    return '{"mac": "' + BYPASS_MAC + '", "ssid": "' + BYPASS_SSID + '"}\n'


@pytest.mark.parametrize("special", ["\t", "\n", "\\", '"'])
@NEEDS_GIT
def test_trusted_scan_flags_quoted_fixture_paths(tmp_path, special):
    """Finding 1: git-quoted paths must not bypass the fixture checks."""
    name = f"tests/fixtures/demo/bad{special}name.json"
    repo, sha = _git_repo(tmp_path, {name: _bypass_fixture_json()})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "MAC" in proc.stdout
    assert "SSID" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_flags_non_utf8_fixture_paths(tmp_path):
    """Finding 1: a non-UTF-8 name keeps its ASCII prefix/suffix checks."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)

    def run(*args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    run("init", "-q")
    raw_dir = os.fsencode(str(repo / "tests" / "fixtures" / "demo"))
    os.makedirs(raw_dir, exist_ok=True)
    fd = os.open(os.path.join(raw_dir, b"bad\xffname.json"), os.O_WRONLY | os.O_CREAT)
    os.write(fd, _bypass_fixture_json().encode())
    os.close(fd)
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
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "MAC" in proc.stdout


# Review round 1 (PR #56): symlinks. The scanner read the link target
# string as the file's content; the working-tree scan instead follows file
# symlinks and applies the rules under the link's own name. The tree scan
# must resolve relative in-tree links and scan the target blob's content
# under the link's name.
@NEEDS_GIT
def test_trusted_scan_follows_symlink_under_link_name(tmp_path):
    """Finding 2: notes.txt -> payload.py is flagged via notes.txt."""
    payload = PW + "=" + "hunter2" + "hunter2" + "\n"
    repo, sha = _git_repo(
        tmp_path, {"payload.py": payload}, links={"notes.txt": "payload.py"}
    )
    # Parity with the working-tree scan, which follows the link: the same
    # bytes under the link's name are a problem.
    assert hygiene_scan.scan_bytes("notes.txt", payload.encode())
    # ... while the target's own Python name stays exempt.
    assert not hygiene_scan.scan_bytes("payload.py", payload.encode())
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "notes.txt" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_follows_fixture_symlink(tmp_path):
    """Finding 2: a fixture-named link gets the fixture-only checks."""
    repo, sha = _git_repo(
        tmp_path,
        {"tests/fixtures/demo/blob": _bypass_fixture_json()},
        links={"tests/fixtures/demo/leak.json": "blob"},
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    # The target blob's content under the link's fixture name: bare MAC and
    # SSID checks apply (not just "not valid JSON" for the target string).
    assert "MAC" in proc.stdout
    assert "SSID" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_follows_symlink_chains(tmp_path):
    """Finding 2: link -> link -> file resolves under the outer link's name."""
    payload = PW + "=" + "hunter2" + "hunter2" + "\n"
    repo, sha = _git_repo(
        tmp_path,
        {"sub/payload.py": payload},
        links={"sub/mid": "payload.py", "outer.txt": "sub/mid"},
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "outer.txt" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_skips_symlink_to_directory(tmp_path):
    """Finding 2: a link to a directory is skipped, like the working tree.

    The directory name itself would be flagged as content, so scanning the
    target string instead of skipping fails this test.
    """
    dirname = ".".join(REAL10)
    repo, sha = _git_repo(
        tmp_path,
        {f"{dirname}/inner.txt": "docs range 192.0.2.1\n"},
        links={"dirlink": dirname},
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "clean" in proc.stdout


@pytest.mark.parametrize(
    "target",
    ["/abs/path", "../escape", "dangling-target", "loop-b"],
)
@NEEDS_GIT
def test_trusted_scan_fails_closed_on_unresolvable_symlink(tmp_path, target):
    """Finding 2: absolute, escaping, dangling, and cyclic links fail closed."""
    links = {"loop-a": "loop-b", "loop-b": "loop-a"} if target == "loop-b" else {"bad": target}
    repo, sha = _git_repo(tmp_path, {"ok.txt": "hello\n"}, links=links)
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "symlink" in proc.stdout.lower()


# Review round 1 (PR #56): the inherited credential regexes backtrack
# badly on long word runs (``b"g" * 100_000`` runs past 5 s, far under the
# 2 MB cap), and the scan had no overall deadline. The patterns must stay
# linear without changing what matches, and the scan must fail closed on a
# wall-clock deadline.
@NEEDS_GIT
def test_trusted_scan_is_fast_on_long_word_runs(tmp_path):
    """Finding 3: 100k word chars plus a real IP scans in seconds, not minutes."""
    planted = ".".join(REAL10)
    repo, sha = _git_repo(tmp_path, {"notes.txt": "g" * 100_000 + "\n" + planted + "\n"})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"IPv4 {planted}" in proc.stdout


def test_credential_scan_is_fast_on_long_word_runs():
    """Finding 3: the finding's repro through scan_bytes, with a generous limit."""
    import time

    planted = ".".join(REAL10)
    text = ("g" * 100_000 + "\n" + planted + "\n").encode()
    start = time.monotonic()
    problems = hygiene_scan.scan_bytes("notes.txt", text)
    elapsed = time.monotonic() - start
    assert any(p.startswith("IPv4") for p in problems)
    assert elapsed < 10, f"scan took {elapsed:.1f}s"


def _old_reference_problems(text, unquoted=True):
    """The pre-fix credential matchers, kept as the differential oracle.

    Built from parts like the module so this file does not match itself.
    """
    import re as _re

    key_words = (
        r"(?:x_\w+|\w*(?:"
        + "pass" + "word|pass" + "wd|passphrase|sec" + "ret|tok" + "en"
        + r"|api[_-]?key|auth[_-]?key|_key))"
    )
    old_credential = _re.compile(
        r"""(?i)["']?""" + key_words + r"""["']?\s*[:=]\s*(?:"([^"\n]{8,})"|'([^'\n]{8,})')"""
    )
    old_url = _re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")
    old_unquoted = _re.compile(
        r"""(?im)^[ \t]*(?:export[ \t]+)?["']?""" + key_words + r"""["']?[ \t]*[:=][ \t]*"""
        r"""([^\s"'#,{}\[\]][^\s#,]{7,})"""
    )
    found = []
    for _match in old_url.finditer(text):
        found.append("likely secret (credentials in a URL)")
    for match in old_credential.finditer(text):
        if PLANTED in match.group(0):
            continue
        value = next((g for g in match.groups() if g), None)
        if value is not None and hygiene_scan.REFERENCE_VALUE.match(value.strip()):
            continue
        # The module reports quoted and unquoted assignments under one label.
        found.append("likely secret (credential assignment)")
    if unquoted:
        for match in old_unquoted.finditer(text):
            if PLANTED in match.group(0):
                continue
            value = match.group(1)
            if hygiene_scan.REFERENCE_VALUE.match(value.strip()):
                continue
            found.append("likely secret (credential assignment)")
    return sorted(found)


def _new_label_problems(text, unquoted=True):
    """The fixed matchers' problems for the same labels, as a multiset."""
    return sorted(
        problem
        for problem in hygiene_scan.secret_problems(text, unquoted=unquoted)
        if problem.endswith("(credentials in a URL)") or problem.endswith("(credential assignment)")
    )


def test_credential_matchers_agree_with_reference():
    """Finding 3: the linear rewrite matches exactly what the old regexes did."""
    import random

    rng = random.Random(20261111)
    keys = [
        "pass" + "word", "PASS" + "WD", "sec" + "ret", "tok" + "en",
        "x_auth" + "key", "api" + "-key", "auth" + "_key", "_key",
        "user", "name", "wordpass", "tokenizer",
    ]
    seps = [":", "=", " : ", " = ", ":", " =\n  "]
    quotes = ['"', "'", ""]
    values = [
        "hunter2" + "hunter2", "short", "x" * 40, "${{ github." + "token }}",
        "$VAR", "<placeholder>", "unifi_pass" + "word", "changeme",
        "abc 123 " + "45678", "-PLANTED-SECRET-9",
    ]
    words = ["g", "gg", "abc123", "http", "://", "user", "pass", "@", " ", "\n", "export "]
    for trial in range(400):
        parts = []
        for _ in range(rng.randint(1, 6)):
            pick = rng.random()
            if pick < 0.45:
                parts.append(rng.choice(keys) + rng.choice(seps) + rng.choice(quotes)
                             + rng.choice(values) + rng.choice(quotes))
            elif pick < 0.6:
                parts.append(rng.choice(["http", "https", "ftp"]) + "://"
                             + rng.choice(["u", "admin", "a.b"]) + ":"
                             + rng.choice(["p", "hunter2" + "hunter2"]) + "@"
                             + rng.choice(["h", "192.0.2.1"]))
            else:
                parts.append(rng.choice(words) * rng.randint(1, 30))
        text = "".join(parts)
        for unquoted in (True, False):
            assert _new_label_problems(text, unquoted) == _old_reference_problems(
                text, unquoted
            ), f"trial {trial}: {text!r} unquoted={unquoted}"


def test_credential_fuzz_long_runs_agree_with_reference():
    """Finding 3: agreement also holds on long adversarial runs."""
    import random

    rng = random.Random(56)
    alphabet = ["g", "a", "1", " ", ":", "=", '"', "pass" + "word", "\n", "@", "/"]
    for trial in range(30):
        text = "".join(rng.choice(alphabet) for _ in range(2000))
        for unquoted in (True, False):
            assert _new_label_problems(text, unquoted) == _old_reference_problems(
                text, unquoted
            ), f"long trial {trial} unquoted={unquoted}"


def test_scan_deadline_fails_closed(monkeypatch):
    """Finding 3: an exhausted wall-clock deadline refuses instead of passing."""
    monkeypatch.setattr(hygiene_scan, "SCAN_DEADLINE_S", 0)
    try:
        hygiene_scan.scan_git_tree("0" * 40, cwd=str(ROOT))
    except hygiene_scan.FailClosed as exc:
        assert "deadline" in str(exc).lower()
    else:
        raise AssertionError("expected FailClosed on an exhausted deadline")


# Review round 1 (PR #56): for a mode-160000 entry the size field is "-",
# and int(size) ran before the type check, so any submodule exited 2. The
# type must be checked first and gitlinks skipped like the working tree.
@NEEDS_GIT
def test_trusted_scan_skips_gitlinks(tmp_path):
    """Finding 5: a submodule entry is skipped and stated, not a crash."""
    repo, sha = _git_repo(tmp_path, {"README.md": "hello\n"})
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{head},sub"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
            "commit", "-qm", "submodule",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert sha2 != sha
    proc = _scan_tree(repo, sha2)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "submodule" in proc.stdout.lower()


# Review round 1 (PR #56): the suppression listing printed the full
# ``path:lineno: line`` into the CI log, leaking the suppressed secret
# (hard rule 6). Only the path and line number may be listed.
@NEEDS_GIT
def test_trusted_scan_suppression_listing_hides_content(tmp_path):
    """Finding 6: a suppressed secret line never reaches the log."""
    line = PW + '="' + "hunter2" + "hunter2" + '"  # ' + SUPPRESS + "\n"
    repo, sha = _git_repo(tmp_path, {"notes.txt": line})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "suppression notes.txt:1" in proc.stdout
    assert "hunter2" not in proc.stdout


# Review round 1 notes: deeply nested JSON raised an uncaught
# RecursionError (non-zero but ugly). It must fail closed with a message.
def test_deeply_nested_fixture_json_fails_closed():
    """Note: 100k-deep JSON is a flagged problem, not an exception."""
    data = ("[" * 100_000 + "]" * 100_000).encode()
    problems = hygiene_scan.scan_bytes("tests/fixtures/demo/deep.json", data)
    assert problems, "expected a fail-closed problem for nested JSON"
    assert any("nested" in problem for problem in problems)
    assert not any("RecursionError" in problem for problem in problems)


# Review round 1 notes: the listing was buffered before the entry cap
# applied, so a compact tree expanding to ~1M paths exhausted memory. The
# cap must be enforced while the listing is read.
def test_tree_entry_cap_enforced_while_streaming(monkeypatch, tmp_path):
    """Note: an endless listing fails closed at the cap, never buffered."""
    repo, sha = _git_repo(tmp_path, {"ok.txt": "hello\n"})

    class EndlessStream:
        def __init__(self):
            self.chunk = b"100644 blob " + b"0" * 40 + b"      1\tf\0"

        def read(self, n):
            return self.chunk * (n // len(self.chunk) + 1)

    class FakeProc:
        def __init__(self):
            self.stdout = EndlessStream()
            self.stderr = None

        def kill(self):
            pass

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(hygiene_scan.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(hygiene_scan, "MAX_TREE_ENTRIES", 100)
    try:
        hygiene_scan.list_tree_blobs(sha, cwd=str(repo))
    except hygiene_scan.FailClosed as exc:
        assert "entries" in str(exc).lower() or "limit" in str(exc).lower()
    else:
        raise AssertionError("expected FailClosed on an over-cap listing")


# Review round 2 (PR #56), finding 1 (BLOCKER): when a tree record was
# split across two 64 KiB reads, the scanner dropped the incomplete tail
# and parsed the next chunk's filename bytes as record metadata, so a
# filename holding forged "100644 blob <clean> ..." text aligned to the
# boundary made the scan read a clean blob instead of the real one. The
# tail is kept across reads; only complete NUL-terminated records parse,
# and every record is validated strictly.
def _sample_listing():
    """Three records: plain, forged metadata inside a name, and a symlink."""
    clean = b"0" * 40
    return (
        b"100644 blob " + b"a" * 40 + b"       2\tok.txt\0"
        + b"100644 blob " + b"b" * 40 + b"      29\ttests/fixtures/100644 blob "
        + clean + b"       2\tordinary.json\0"
        + b"120000 blob " + b"c" * 40 + b"       9\tlink.txt\0"
    )


def test_tree_records_survive_splits_at_every_offset():
    """Finding 1 (round 2): a record split at any byte still parses whole."""
    whole = _sample_listing()
    expected = list(hygiene_scan._iter_tree_records([whole]))
    assert len(expected) == 3
    for cut in range(1, len(whole)):
        split = list(hygiene_scan._iter_tree_records([whole[:cut], whole[cut:]]))
        assert split == expected, f"split at byte {cut}"
    mode, objtype, sha, size, path = hygiene_scan._parse_tree_record(expected[0])
    assert (mode, objtype, sha, size, path) == ("100644", "blob", "a" * 40, 2, "ok.txt")
    _, _, _, _, forged_path = hygiene_scan._parse_tree_record(expected[1])
    assert forged_path == (
        "tests/fixtures/100644 blob " + "0" * 40 + "       2\tordinary.json"
    )


def test_tree_record_stream_rejects_unterminated_tail():
    """Finding 1 (round 2): trailing bytes without a NUL fail closed."""
    with pytest.raises(hygiene_scan.FailClosed):
        list(hygiene_scan._iter_tree_records([b"100644 blob " + b"a" * 40]))


@pytest.mark.parametrize(
    "record",
    [
        b"",
        b"100644 blob " + b"a" * 40 + b"       2ok.txt",
        b"100644 tree " + b"a" * 40 + b"       2\tdir",
        b"120000 commit " + b"a" * 40 + b"       2\tl",
        b"160000 blob " + b"a" * 40 + b"       -\tsub",
        b"10064 blob " + b"a" * 40 + b"       2\tshort",
        b"100755 blob " + b"xyz" + b"       2\tshort",
        b"100644 blob " + b"a" * 40 + b"       X\tbadsize",
        b"100644 blob " + b"a" * 40 + b"       -\tnegsize",
        b"100644 blob " + b"a" * 40 + b"       2\t",
        b"100644 blob " + b"\xff" * 40 + b"       2\tnonascii",
    ],
)
def test_tree_record_parser_rejects_malformed(record):
    """Finding 1 (round 2): anything unexpected fails closed, never guessed."""
    with pytest.raises(hygiene_scan.FailClosed):
        hygiene_scan._parse_tree_record(record)


@NEEDS_GIT
def test_trusted_scan_split_records_still_flag_the_leak(tmp_path, monkeypatch):
    """Finding 1 (round 2): tiny reads split every record; the leak is flagged."""
    monkeypatch.setattr(hygiene_scan, "_LS_TREE_CHUNK", 7)
    leak = PW + '="' + "hunter2" + "hunter2" + '"\n'
    repo, sha = _git_repo(tmp_path, {"notes.txt": leak, "other.txt": "hello\n"})
    problems, _, _, _, _ = hygiene_scan.scan_git_tree(sha, cwd=str(repo))
    assert any("credential assignment" in p for p in problems), problems


@NEEDS_GIT
def test_trusted_scan_forged_metadata_at_read_boundary(tmp_path):
    """Finding 1 (round 2): forged metadata aligned to byte 65536 still flags."""
    leak = (PW + '="' + "hunter2" + "hunter2" + '"\n').encode()
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)

    def run(*args, **kwargs):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, **kwargs
        )

    run("init", "-q")
    for n in range(920):
        (repo / f"a{n:04d}.txt").write_bytes(b"ok")
    (repo / ("b" * 78)).write_bytes(b"ok")
    safe = run("hash-object", "--stdin", input=b"ok").stdout.decode().strip()
    spoof = f"100644 blob {safe}       2\tordinary.json"
    (repo / "tests" / "fixtures").mkdir(parents=True)
    (repo / "tests" / "fixtures" / spoof).write_bytes(leak)
    run("add", "-A")
    run(
        "-c", "user.name=test",
        "-c", "user.email=test@example.invalid",
        "commit", "-qm", "test",
    )
    sha = run("rev-parse", "HEAD").stdout.decode().strip()
    raw = run("ls-tree", "-r", "-z", "--long", sha).stdout
    # The forged filename itself must start exactly at the 64 KiB read cut.
    assert raw[65536:].startswith(spoof.encode()), "repro alignment broke"
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "credential assignment" in proc.stdout


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
