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
import re
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
    assert scan(flagged) == ["IPv4 address at line 2"]


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
    assert "IPv4 address at line 1" in proc.stdout
    assert planted not in proc.stdout


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
    assert "IPv4 address at line 1" in proc.stdout
    assert planted not in proc.stdout


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
    assert "IPv4 address at line 1" in proc.stdout
    assert planted not in proc.stdout


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


# Review round 3 (PR #56): symlinks and submodules are not allowed in this
# repo. Resolving links produced a blocker in every review round (a
# submodule escape slipped past the scan; a repeated alias was rejected as
# a cycle), so any symlink entry (mode 120000) or gitlink entry (mode
# 160000) anywhere in the head tree fails the check outright, with a fixed
# message naming only the entry's path. Each case below must exit 2: a
# plain link to a file, a link to a directory, a dangling link, an absolute
# or escaping target, a link cycle, a link named like a fixture, and a
# nested-directory link.
@pytest.mark.parametrize(
    "files,links",
    [
        ({"payload.py": "hello\n"}, {"notes.txt": "payload.py"}),
        ({"subdir/inner.txt": "hello\n"}, {"dirlink": "subdir"}),
        ({"ok.txt": "hello\n"}, {"bad": "dangling-target"}),
        ({"ok.txt": "hello\n"}, {"bad": "/abs/path"}),
        ({"ok.txt": "hello\n"}, {"sub/bad": "../escape"}),
        ({"ok.txt": "hello\n"}, {"loop-a": "loop-b", "loop-b": "loop-a"}),
        (
            {"blob": "hello\n"},
            {"tests/fixtures/demo/leak.json": "blob"},
        ),
        (
            {"dir/sub/payload.py": "hello\n"},
            {"dir/mid": "payload.py", "outer.txt": "dir/mid"},
        ),
    ],
)
@NEEDS_GIT
def test_trusted_scan_rejects_any_symlink(tmp_path, files, links):
    """Round 3: every symlink shape fails closed, never resolved."""
    repo, sha = _git_repo(tmp_path, files, links=links)
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "symlinks and submodules are not allowed" in proc.stdout


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
    assert "IPv4 address at line 2" in proc.stdout
    assert planted not in proc.stdout


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
    def at(match):
        return text.count("\n", 0, match.start()) + 1

    found = []
    for _match in old_url.finditer(text):
        found.append(("likely secret (credentials in a URL)", at(_match)))
    for match in old_credential.finditer(text):
        if PLANTED in match.group(0):
            continue
        value = next((g for g in match.groups() if g), None)
        if value is not None and hygiene_scan.REFERENCE_VALUE.match(value.strip()):
            continue
        # The module reports quoted and unquoted assignments under one label.
        found.append(("likely secret (credential assignment)", at(match)))
    if unquoted:
        for match in old_unquoted.finditer(text):
            if PLANTED in match.group(0):
                continue
            value = match.group(1)
            if hygiene_scan.REFERENCE_VALUE.match(value.strip()):
                continue
            found.append(("likely secret (credential assignment)", at(match)))
    return sorted(found)


def _new_label_problems(text, unquoted=True):
    """The fixed matchers' (label, line) pairs for the same labels."""
    import re as _re

    parsed = []
    for problem in hygiene_scan.secret_problems(text, unquoted=unquoted):
        match = _re.fullmatch(r"(likely secret \(.*\)) at line (\d+)", problem)
        if match is None:
            continue
        label, lineno = match.groups()
        if label.endswith("(credentials in a URL)") or label.endswith(
            "(credential assignment)"
        ):
            parsed.append((label, int(lineno)))
    return sorted(parsed)


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


# Review round 3 (PR #56): gitlinks are not allowed in this repo either.
# A mode-160000 entry used to be skipped silently (round 1) and later let a
# submodule escape slip past the link resolver (round 3, finding 1); now any
# gitlink anywhere in the head tree fails the check outright.
@NEEDS_GIT
def test_trusted_scan_rejects_any_gitlink(tmp_path):
    """Round 3: a submodule entry fails closed, never skipped."""
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
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "symlinks and submodules are not allowed" in proc.stdout


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
    problems, _, _, _, _, _ = hygiene_scan.scan_git_tree(sha, cwd=str(repo))
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


# Review round 2 (PR #56), finding 3 (SHOULD-FIX): the JSON-web-token and
# session-cookie patterns scan quadratically (``b"eyJ" * n`` takes
# 0.25/1.0/4.0 s at 30/60/120 KB; ``"token=-" * 20_000`` exceeds 10 s), and
# the 300 s deadline is checked only between blobs, so a single blob can
# run past it and expiry during the last blob still passes. The matchers
# below are linear without changing their match sets, pinned by a
# differential fuzz against the naive regexes; every pattern gets an
# adversarial performance test; and the deadline is also checked after the
# last blob and before reporting success (plus a step-level
# ``timeout-minutes`` in ci-guard.yml so GitHub kills a stalled scan).
def _old_spans(pattern, text):
    return [(m.start(), m.end()) for m in pattern.finditer(text)]


def _old_jwt_re():
    import re as _re

    return _re.compile("ey" + "J[A-Za-z0-9_-]{8,}\\.ey" + "J[A-Za-z0-9_-]{8,}")


def _old_cookie_re():
    import re as _re

    return _re.compile(
        r"(?i)\b(?:unifises|token|csrf_token|session(?:id)?)=(?=[^;\s\"']*\d)[^;\s\"']{8,}"
    )


def test_jwt_spans_agree_with_naive_reference():
    """Finding 3 (round 2): the linear JWT matcher finds the same spans."""
    import random

    old = _old_jwt_re()
    rng = random.Random(20261112)
    alphabet = [
        "eyJ", "e", "y", "J", "ey", "yJ", "A", "0", "-", "_", ".", "=", " ",
        "\n", "eyJeyJ", "eyJJ", "eJ", "eyJ.",
    ]
    for trial in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(300))
        assert hygiene_scan._jwt_token_spans(text) == _old_spans(old, text), (
            f"trial {trial}: {text!r}"
        )


def test_jwt_spans_agree_on_adversarial_runs():
    """Finding 3 (round 2): agreement on the shapes that backtrack badly."""
    old = _old_jwt_re()
    heads = ["eyJ", ".eyJ", "ey", "e", ""]
    mids = ["A", "A" * 7, "A" * 8, "A" * 100, "-_09az", ".", "..", ".eyJ"]
    tails = ["", ".eyJ", ".eyJ" + "B" * 7, ".eyJB" * 8, ".eyJ" + "B" * 100, "!"]
    for head in heads:
        for mid in mids:
            for tail in tails:
                text = head + mid + tail
                assert hygiene_scan._jwt_token_spans(text) == _old_spans(old, text), (
                    repr(text)
                )
    assert hygiene_scan._jwt_token_spans("eyJ" * 2000) == _old_spans(
        old, "eyJ" * 2000
    )


def test_session_cookie_spans_agree_with_naive_reference():
    """Finding 3 (round 2): the linear cookie matcher finds the same spans."""
    import random

    old = _old_cookie_re()
    rng = random.Random(20261113)
    alphabet = [
        "token", "TOKEN", "Token", "session", "sessionid", "csrf_token",
        "unifises", "tok", "oken", "mytoken", "=", ";", " ", "\"", "'",
        "-", "_", "abc123", "12345678", "x" * 20, "\n", "tokeſ", "ß",
    ]
    for trial in range(300):
        text = "".join(rng.choice(alphabet) for _ in range(40))
        assert hygiene_scan._session_cookie_spans(text) == _old_spans(
            old, text
        ), f"trial {trial}: {text!r}"


def test_session_cookie_spans_agree_on_adversarial_runs():
    """Finding 3 (round 2): agreement on the shapes that backtrack badly."""
    old = _old_cookie_re()
    for text in [
        "token=-" * 2000,
        "token=" + "x" * 2000,
        "token=" + "1" * 2000,
        ("token=" + "x" * 7 + ";") * 500,
        "mytoken=12345678",
        "csrf_token=" + "12345678",
        "TOKEN=" + "12345678",
        "sessionid=" + "12345678",
        "token=" + "12345678 token=abcdefgh",
        "token=" + "12345678;token=abcdefgh",
        # A digit before the value is not the lookahead's digit.
        "1-token=abcdefgh",
        "12token=abcdefgh",
        "token123=abcdefgh",
    ]:
        assert hygiene_scan._session_cookie_spans(text) == _old_spans(old, text), (
            repr(text[:80])
        )


def test_planted_jwt_and_cookie_are_still_skipped():
    """Finding 3 (round 2): PLANTED-SECRET still exempts the new matchers."""
    jwt = "eyJ" + "A" * 8 + ".eyJ" + "B" * 8
    assert hygiene_scan.secret_problems(jwt)
    assert not hygiene_scan.secret_problems(jwt.replace("AAAA", "PLANTED-SECRET"))
    cookie = "token=" + "x" * 4 + "PLANTED-SECRET" + "y" * 4
    assert not hygiene_scan.secret_problems(cookie)
    assert hygiene_scan.secret_problems("token=" + "x" * 4 + "1234" + "y" * 4)


def test_every_pattern_is_fast_on_adversarial_input():
    """Finding 3 (round 2): every pattern's literal bait at ~200 KB < 2 s."""
    import time

    baits = {
        "private key": "-----BEGIN ",
        "Muse SDK token": "mgst_",
        "GitHub token": "ghp_",
        "AWS access key": "AKIA",
        "Slack token": "xoxb-",
        "credentials in a URL": "https://",
        "JSON web token": "eyJ",
        "bearer token": "bearer ",
        "session cookie": "token=-",
        "CSRF token value": "x-csrf-token: ",
        "credential assignment": 'pass' + 'word="',
        "MAC": "ab:",
        "IPv4": "1.",
    }
    labels = {label for label, _ in hygiene_scan.SECRET_PATTERNS}
    assert set(baits) >= labels | {"MAC", "IPv4"}, (
        "perf test must cover every pattern: " + repr(sorted(labels))
    )
    for label, bait in sorted(baits.items()):
        if label == "MAC":
            text = (bait * 50000)[:200000]
            problems = lambda t: hygiene_scan.mac_problems(t)  # noqa: E731
        elif label == "IPv4":
            text = (bait * 100000)[:200000]
            problems = lambda t: hygiene_scan.ip_problems(t)  # noqa: E731
        elif label == "credential assignment":
            text = ((bait + "x" * 7 + "\n") * 15000)[:200000]
            problems = lambda t: hygiene_scan.secret_problems(t)  # noqa: E731
        else:
            text = (bait * ((200000 // len(bait)) + 1))[:200000]
            problems = lambda t: hygiene_scan.secret_problems(t)  # noqa: E731
        start = time.monotonic()
        problems(text)
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"{label}: {elapsed:.1f}s on 200 KB of {bait!r}"


def test_scan_deadline_checked_before_reporting_clean(tmp_path, monkeypatch):
    """Finding 3 (round 2): expiry during the last blob still refuses."""
    repo, sha = _git_repo(tmp_path, {"ok.txt": "hello\n"})
    calls = []

    def spy(start, what):
        calls.append(what)
        if what == "finishing the scan":
            raise hygiene_scan.FailClosed("spy: expired at the end")

    monkeypatch.setattr(hygiene_scan, "_check_deadline", spy)
    with pytest.raises(hygiene_scan.FailClosed):
        hygiene_scan.scan_git_tree(sha, cwd=str(repo))
    assert "finishing the scan" in calls


def test_line_lookup_matches_newline_count():
    """Finding 2 (round 3): the bisect lookup reports the old count's lines."""
    import random

    rng = random.Random(20261114)
    for _ in range(200):
        text = "".join(
            rng.choice(["a", "\n", "ab\n", "\r", "x" * 50])
            for _ in range(rng.randint(1, 50))
        )
        line_of = hygiene_scan._line_lookup(text)
        for _ in range(20):
            pos = rng.randint(0, len(text))
            assert line_of(pos) == text.count("\n", 0, pos) + 1, (
                f"{text!r} at {pos}"
            )


def test_ip_fast_paths_agree_with_ipaddress_reference():
    """Finding 2 (round 3): integer/head checks decide exactly what parses did."""
    import ipaddress
    import random

    rng = random.Random(20261115)
    quads = [
        ["192", "0", "2", "0"], ["192", "0", "2", "255"], ["192", "0", "3", "0"],
        ["191", "255", "255", "255"], ["198", "51", "100", "0"],
        ["198", "51", "100", "255"], ["198", "51", "101", "0"],
        ["203", "0", "113", "0"], ["203", "0", "113", "255"],
        ["203", "0", "114", "0"], ["127", "0", "0", "0"],
        ["127", "255", "255", "255"], ["128", "0", "0", "0"],
        ["0", "0", "0", "0"], ["0", "0", "0", "1"], ["255", "0", "0", "0"],
        ["255", "255", "255", "255"], ["10", "1", "1", "1"],
        ["010", "000", "000", "001"], ["6", "6", "77", "15402"],
        ["1", "2", "3", "256"], ["999", "1", "1", "1"],
    ]
    for _ in range(500):
        quads.append([str(rng.randint(0, 999)) for _ in range(4)])
    # The oracle replays the old slow path per regex match: the fast path
    # may only change how a matched candidate is decided, never what the
    # regex matches or which matches are skipped.
    for quad in quads:
        text = ".".join(quad) + "\n"
        got = hygiene_scan.ip_problems(text)
        expected = []
        for found in hygiene_scan.IPV4_RE.finditer(text):
            parts = [int(p) for p in found.group(0).split(".")]
            if any(p > 255 for p in parts):
                continue
            addr = ipaddress.ip_address(".".join(map(str, parts)))
            if not any(addr in net for net in hygiene_scan.ALLOWED_NETS):
                expected.append("IPv4 address at line 1")
        assert got == expected, quad
    hex4 = ["0", "00", "1", "2001", "db8", "DB8", "ff00", "FF02", "fe80",
            "ffff", "0000", "0001", "abcd", "1234"]
    # Adversarial colon shapes: trailing/leading single colons, doubled
    # compression, bare and trailing compression.
    hard_shapes = [
        "fe80::02:00:00:", "1:2:3:4:5:6:7:", ":1:2:3:4:5:6:7", "1::2::",
        # One shape is built at run time like the other invented values: a
        # short compressed address is valid and non-documentation, so its
        # literal cannot sit here.
        "::", "1::", "::1", "1:2" + "::", "::ffff:192.0.2.1",
        "2001:db8::1", "2001:db8:::1",
    ]
    v6_texts = []
    for _ in range(500):
        groups = [rng.choice(hex4) for _ in range(rng.randint(1, 8))]
        text = ":".join(groups)
        if rng.random() < 0.5:
            at = rng.randint(0, len(groups))
            text = ":".join(groups[:at]) + "::" + ":".join(groups[at:])
        v6_texts.append(text + "\n")
    v6_texts.extend(shape + "\n" for shape in hard_shapes)
    for text in v6_texts:
        got = hygiene_scan.ip_problems(text)
        expected = []
        for found in hygiene_scan.IPV6_RE.finditer(text):
            candidate = found.group(0)
            if sum(1 for group in candidate.split(":") if group) < 2:
                continue
            try:
                addr = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not any(addr in net for net in hygiene_scan.ALLOWED_NETS):
                expected.append("IPv6 address at line 1")
        assert got == expected, repr(text)


def test_ip_fast_path_matches_ipaddress_without_2001_heads():
    """Finding 2 (round 4): triple-colon candidates take the slow path.

    ``candidate.count("::")`` counts non-overlapping occurrences, so
    triple-colon candidates hold exactly one ``::`` and the fast
    path flagged addresses ``ipaddress`` rejects. The round-3 generator
    above draws heads from a list holding ``2001``, which forces the slow
    path; this generator never starts a candidate with a slow-path head
    (``2001``, ``ff00``-``ffff``, empty, or all-zero), so the fast path
    decides, plus the exact regression shapes.
    """
    import ipaddress
    import random

    rng = random.Random(20261111)
    heads = ["fe80", "2002", "abcd", "1234", "1", "db8", "DB8", "9"]
    tails = ["0", "00", "1", "0001", "abcd", "1234", "fe80", "db8", "ffff"]
    texts = [head + ":::" + tail + "\n" for head, tail in (
        ("fe80", "1"), ("fe80", ""), ("2002", "abcd"), ("1", "2"), ("ab", "cd:1")
    )]
    for _ in range(500):
        groups = [rng.choice(heads)] + [
            rng.choice(tails) for _ in range(rng.randint(1, 7))
        ]
        text = ":".join(groups)
        if rng.random() < 0.5:
            at = rng.randint(0, len(groups))
            text = ":".join(groups[:at]) + "::" + ":".join(groups[at:])
        texts.append(text + "\n")
    for text in texts:
        got = hygiene_scan.ip_problems(text)
        expected = []
        for found in hygiene_scan.IPV6_RE.finditer(text):
            candidate = found.group(0)
            if sum(1 for group in candidate.split(":") if group) < 2:
                continue
            try:
                addr = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not any(addr in net for net in hygiene_scan.ALLOWED_NETS):
                expected.append("IPv6 address at line 1")
        assert got == expected, repr(text)


# Review round 3 (PR #56), finding 2 (SHOULD-FIX): every problem recounts
# newlines from the start of the file (``text.count`` per match), so a 2 MB
# file of repeated hits took ~52 s; and decode sliced the remaining batch
# buffer per blob, so a clean tree at the old caps ran past 355 s. Line
# offsets are precomputed once per file and the batch is walked with a
# cursor; per-check collection stops at MAX_PROBLEMS_PER_CHECK with an exact
# remainder count.
@pytest.mark.parametrize(
    "line",
    [
        # Built at run time: bare documentation-range-exempt literals and
        # bare credential-looking literals must never appear in the repo.
        # The IP line hits the address check; the token line hits the
        # token-format check.
        ".".join(["10", "1", "1", "1"]) + "\n",
        "ey" + "J" + "A" * 8 + ".ey" + "J" + "B" * 8 + "\n",
    ],
)
def test_scan_bytes_is_fast_on_a_2mb_file_of_repeated_hits(line):
    """Finding 2 (round 3): 2 MB of repeated hits scans in under 2 s."""
    import time

    data = (line * ((2_000_000 // len(line)) + 1))[:2_000_000].encode()
    assert len(data) == 2_000_000
    start = time.monotonic()
    problems = hygiene_scan.scan_bytes("notes.txt", data)
    elapsed = time.monotonic() - start
    assert problems, "expected the repeated hits to be flagged"
    assert problems[-1].startswith("... and "), problems[-2:]
    assert elapsed < 2.0, f"scan took {elapsed:.1f}s"


@NEEDS_GIT
@pytest.mark.skipif(
    os.environ.get("HOUSESITTER_SCAN_CEILING") != "1",
    reason="ceiling probe only: run with HOUSESITTER_SCAN_CEILING=1",
)
def test_max_cap_tree_completes_well_under_deadline(tmp_path):
    """Finding 2 (round 3): exactly the new caps: 10,000 entries, 32 MB.

    Builds the ceiling tree without creating 10,000 disk files (one blob,
    referenced 10,000 times through ``git mktree``) and scans it in-process.
    Skipped by default so CI never pays for it; run it to report the timing.
    """
    import time

    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args, **kwargs):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, **kwargs
        )

    run("init", "-q")
    blob = run("hash-object", "-w", "--stdin", input=b"x" * 3199 + b"\n")
    blob_sha = blob.stdout.decode().strip()
    entry = f"100644 blob {blob_sha}\t".encode()
    tree_input = b"".join(entry + f"f{n:05d}".encode() + b"\n" for n in range(10_000))
    sha = run("mktree", input=tree_input).stdout.decode().strip()
    start = time.monotonic()
    (
        problems,
        problem_total,
        suppressions,
        suppression_total,
        files_scanned,
        bytes_scanned,
    ) = hygiene_scan.scan_git_tree(sha, cwd=str(repo))
    elapsed = time.monotonic() - start
    print(f"\nceiling: 10,000 entries / 32,000,000 bytes in {elapsed:.1f}s")
    assert (files_scanned, bytes_scanned) == (10_000, 32_000_000)
    assert problems == [], problems[:3]
    assert suppressions == []
    assert problem_total == 0
    assert suppression_total == 0
    assert elapsed < 120, f"ceiling scan took {elapsed:.1f}s"


def test_counted_totals_match_underlying_findings():
    """Finding 3 (round 4): totals count findings, not capped messages."""
    import json

    # Built at run time: bare documentation-range-exempt literals must never
    # appear in the repo.
    loop4 = ".".join(["10", "1", "1", "1"]) + "\n"
    loop_mac = ":".join(REAL_MAC) + "\n"
    messages, total = hygiene_scan._ip_problems_counted(loop4 * 2000)
    assert total == 2000
    assert messages[-1] == "... and 1000 more IP address problem(s) in this file"
    assert len(messages) == 1001
    messages, total = hygiene_scan._mac_problems_counted(loop_mac * 2000, bare=True)
    assert total == 2000
    assert messages[-1] == "... and 1000 more MAC address problem(s) in this file"
    assert len(messages) == 1001
    messages, total = hygiene_scan._secret_problems_counted(("ghp_" + "A" * 30 + "\n") * 2000)
    assert total == 2000
    assert messages[-1] == "... and 1000 more likely-secret problem(s) in this file"
    assert len(messages) == 1001
    messages, total = hygiene_scan._scan_text_counted("notes.txt", loop4 * 2000)
    assert total == 2000
    assert len(messages) == 1001
    fixture = json.dumps([{"ssid": "bad"}] * 2000)
    messages, total = hygiene_scan._scan_text_counted("tests/fixtures/t.json", fixture)
    assert total == 2000
    assert messages[-1] == "... and 1000 more SSID problem(s) in this file"
    assert len(messages) == 1001
    planted = json.dumps([{"ssid": "bad"}])
    messages, total = hygiene_scan._scan_text_counted("tests/fixtures/t.json", planted)
    assert (messages, total) == (["SSID value (3 chars) at line 1 does not end in -example"], 1)
    messages, total = hygiene_scan._scan_text_counted(
        "tests/fixtures/t.json", "not json"
    )
    assert (messages, total) == (["fixture is not valid JSON"], 1)


@NEEDS_GIT
def test_cli_totals_count_underlying_findings(tmp_path):
    """Finding 3 (round 4): the CLI reports 2,000 findings, not 1,001."""
    loop4 = ".".join(["10", "1", "1", "1"]) + "\n"
    repo, sha = _git_repo(tmp_path, {"notes.txt": loop4 * 2000})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "::error::hygiene-scan: ... and 1800 more problem(s)" in proc.stdout
    assert "::error::hygiene-scan: 2000 problem(s) found" in proc.stdout


# Review round 2 (PR #56), finding 5 (SHOULD-FIX): problem messages
# printed a fixture's whole offending value (a 500,000-character SSID
# produced ~500 KB of stdout), and printed paths raw. Messages now name
# the rule and ``path:line`` and never print the value (at most a short,
# redacted shape); paths are escaped and cut to 200 characters; total
# stdout is capped.
def test_problem_messages_hide_values():
    """Finding 5 (round 2): every problem points, never quotes."""
    import re as _re

    mac = ":".join(REAL_MAC)
    ip4 = ".".join(REAL_IP)
    ip6 = "fe80" + ":" + ":1"
    secret = PW + '="' + "hunter2" + "hunter2" + '"'
    jwt = "eyJ" + "A" * 8 + ".eyJ" + "B" * 8
    cookie = "session" + "=12345678"
    text = "\n".join([mac, ip4, ip6, secret, jwt, cookie]) + "\n"
    problems = hygiene_scan.scan_bytes("notes.txt", text.encode())
    # MAC, IPv4, IPv6, one quoted assignment, one JWT, one cookie:
    # `session` is not a credential keyword, so the unquoted matcher stays out.
    assert len(problems) == 6, problems
    for value in (mac, ip4, ip6, "hunter2" + "hunter2", jwt, cookie):
        assert all(value not in problem for problem in problems), problems
    for problem in problems:
        assert _re.fullmatch(r".* at line \d+", problem), problem
    by_line = {}
    for problem in problems:
        by_line.setdefault(problem.rsplit(" ", 1)[1], []).append(problem)
    assert sorted(by_line, key=int) == ["1", "2", "3", "4", "5", "6"]


def test_ssid_problem_hides_value():
    """Finding 5 (round 2): the SSID message carries a shape, not the name."""
    value = "leak" + "-bad"
    data = ('{"ssid": "' + value + '"}').encode()
    problems = hygiene_scan.scan_bytes("tests/fixtures/demo/x.json", data)
    assert problems == [
        f"SSID value ({len(value)} chars) at line 1 does not end in -example"
    ]
    assert value not in problems[0]


def test_display_hides_unsafe_paths():
    for path in ("a" * 300, "a\udcff", "a\x1bb", "a\nb", "a\tb", "caf\u00e9"):
        assert re.fullmatch(r"\[path [1-9][0-9]*\]", hygiene_scan._display(path))


def test_print_capped_respects_budget(capsys):
    """Finding 5 (round 2): total stdout stops at the budget with a marker."""
    hygiene_scan._print_capped(["x" * 60_000, "y" * 60_000])
    out = capsys.readouterr().out
    assert len(out.encode("utf-8")) < 100_000
    assert "truncated" in out
    assert out.count("\n") == 2


def test_print_capped_budgets_bytes_not_characters(capsys):
    """Finding 6 (round 3): multibyte lines stop at the byte budget."""
    line = "caf\u00e9 " * 100  # 600 chars, 700 bytes per line
    lines = [f"hygiene-scan: suppression dir/{line}:{n}" for n in range(1000)]
    chars = sum(len(entry) + 1 for entry in lines)
    assert chars > 100_000  # under the old character count this fit
    hygiene_scan._print_capped(lines)
    out = capsys.readouterr().out
    raw = out.encode("utf-8")
    assert len(raw) <= 100_000
    assert "truncated" in out
    assert "bytes" in out


def _display_id(path: str) -> str:
    """Display number assigned in this test process."""
    return hygiene_scan._display(path)


def test_display_replaces_secret_shaped_names_with_stable_ids():
    """Secret-shaped names get distinct, repeatable run-local numbers."""
    token_a = "gh" + "p_" + "Q" * 30
    token_b = "mg" + "st_" + "R" * 30
    sensitive = [
        token_a,
        f"dir/{token_a}.txt",
        PW + '="hunter2' + 'hunter2"',
        "dir/" + PW + "=AbCd123456.txt",
        "dir/X_AUTH" + "KEY=AbCd123456.txt",
        "dir/" + token_b,
    ]
    for path in sensitive:
        shown = hygiene_scan._display(path)
        assert shown == _display_id(path), (path, shown)
        for secret in (token_a, token_b, PW, "AbCd123456"):
            assert secret not in shown, (path, shown)
    # Distinct names get distinct ids; repeats are stable.
    assert hygiene_scan._display(token_a) != hygiene_scan._display(token_b)
    assert hygiene_scan._display(token_a) == hygiene_scan._display(token_a)
    # Safe ordinary and planted names print in full; assignments stay opaque.
    assert hygiene_scan._display("notes.txt") == "notes.txt"
    assert hygiene_scan._display("PLANTED-SECRET-notes") == "PLANTED-SECRET-notes"
    assert re.fullmatch(r"\[path [1-9][0-9]*\]", hygiene_scan._display(PW + "=changeme"))


@NEEDS_GIT
def test_single_huge_suppression_file_scans_in_ci_time(tmp_path):
    """Finding 4 (round 4): one 2 MB suppression file under a deep path < 2 s.

    The reviewer's full 32 MB ceiling (16 such files under 20 path
    components) hit the 300 s deadline because every suppression line
    re-redacted and re-escaped the whole path. The path is now formatted
    once per file and only a bounded number of entries is kept, so the
    single-file repro below runs in CI time; the full 32 MB timing is
    reported in the PR.
    """
    import time

    deep = "/".join(["d" * 190] * 20)
    rel = f"{deep}/f0.txt"
    data = ("hygiene: ok\n" * ((2_000_000 // len("hygiene: ok\n")) + 1))[:2_000_000]
    repo, sha = _git_repo(tmp_path, {rel: data})
    start = time.monotonic()
    (
        problems,
        problem_total,
        suppressions,
        suppression_total,
        files_scanned,
        bytes_scanned,
    ) = hygiene_scan.scan_git_tree(sha, cwd=str(repo))
    elapsed = time.monotonic() - start
    assert (problems, problem_total, files_scanned, bytes_scanned) == ([], 0, 1, 2_000_000)
    assert suppression_total == sum(1 for line in data.splitlines() if "hygiene: ok" in line)
    assert len(suppressions) == hygiene_scan.MAX_LISTED_SUPPRESSIONS
    assert elapsed < 2.0, f"single-file repro took {elapsed:.1f}s"


@NEEDS_GIT
def test_trusted_scan_suppression_listing_redacts_secret_name(tmp_path):
    """Findings 1 and 5 (round 4): the suppression path hides every shape."""
    token = "gh" + "p_" + "Q" * 30
    nested = "dir/" + PW + "=AbCd123456.txt"
    repo, sha = _git_repo(
        tmp_path, {token: "# hygiene: ok\n", nested: "# hygiene: ok\n"}
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert token not in proc.stdout
    assert PW not in proc.stdout
    assert "AbCd123456" not in proc.stdout
    assert "suppression [path 2]:1" in proc.stdout
    assert "suppression [path 1]:1" in proc.stdout
    assert _display_id(token) != _display_id(nested)


@NEEDS_GIT
def test_trusted_scan_problem_listing_redacts_nested_secret_name(tmp_path):
    """Findings 1 and 5 (round 4): problem lines hide a nested secret name."""
    nested = "dir/" + PW + "=AbCd123456.txt"
    planted = "server at " + ".".join(REAL10) + "\n"
    repo, sha = _git_repo(tmp_path, {nested: planted})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert PW not in proc.stdout
    assert "AbCd123456" not in proc.stdout
    assert "hygiene-scan: [path 1]: IPv4 address at line 1" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_symlink_error_redacts_nested_secret_name(tmp_path):
    """Findings 1 and 5 (round 4): the symlink error hides the entry name."""
    link = "dir/" + PW + "=AbCd123456.txt"
    repo, sha = _git_repo(tmp_path, {"target.txt": "hello\n"}, links={link: "target.txt"})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert PW not in proc.stdout
    assert "AbCd123456" not in proc.stdout
    assert "[path 1]" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_oversized_blob_error_redacts_secret_name(tmp_path):
    """Finding 1 (round 4): the fail-closed path hides a token-shaped name."""
    token = "gh" + "p_" + "Q" * 30
    repo, sha = _git_repo(tmp_path, {token: "Z" * (hygiene_scan.MAX_FILE_BYTES + 1)})
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "fail closed" in proc.stdout
    assert token not in proc.stdout
    assert "[path 1]" in proc.stdout


@NEEDS_GIT
def test_trusted_scan_huge_ssid_stays_small(tmp_path):
    """Finding 5 (round 2): a 500 KB SSID becomes one short line."""
    value = "Z" * 500000
    repo, sha = _git_repo(
        tmp_path, {"tests/fixtures/huge.json": '{"ssid": "' + value + '"}'}
    )
    proc = _scan_tree(repo, sha)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert value not in proc.stdout
    assert f"SSID value ({len(value)} chars)" in proc.stdout
    assert len(proc.stdout) < 100_000


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
    assert "IPv4 address at line 1" in proc.stdout
    assert planted not in proc.stdout

@pytest.mark.parametrize(
    "boundary", ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
@pytest.mark.parametrize("channel", ["suppression", "problem", "oversize", "symlink", "submodule"])
@NEEDS_GIT
def test_path_boundaries_never_disclose_secrets(tmp_path, boundary, channel):
    """PR #61: every output channel hides splitlines credential paths."""
    value = "AbCd" + "123456"
    name = "dir/" + boundary + PW + "=" + value + ".txt"
    data = {
        "suppression": "# hygiene: ok\n",
        "problem": ".".join(REAL10) + "\n",
        "oversize": "Z" * (hygiene_scan.MAX_FILE_BYTES + 1),
        "symlink": "",
        "submodule": "",
    }[channel]
    if channel == "symlink":
        repo, sha = _git_repo(tmp_path, {"target.txt": "hello\n"}, links={name: "target.txt"})
    elif channel == "submodule":
        repo, _ = _git_repo(tmp_path, {"target.txt": "hello\n"})
        entry = f"160000 commit {'1' * 40}\t{boundary}{PW}={value}.txt\0"
        sub = subprocess.check_output(
            ["git", "mktree", "-z", "--missing"], cwd=repo, input=entry.encode()
        ).decode().strip()
        sha = subprocess.check_output(
            ["git", "mktree"], cwd=repo, input=f"040000 tree {sub}\tdir\n".encode()
        ).decode().strip()
    else:
        repo, sha = _git_repo(tmp_path, {name: data})
    proc = _scan_tree(repo, sha)
    output = proc.stdout + proc.stderr
    assert proc.returncode == (0 if channel == "suppression" else 1 if channel == "problem" else 2)
    assert "[path 1]" in output
    for part in (value, value[:4], value[4:]):
        assert part not in output


def test_display_current_tree_verbatim():
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    for raw in paths.split(b"\0"):
        if raw:
            path = os.fsdecode(raw)
            assert hygiene_scan._display(path) == path


def test_display_uses_run_local_counters():
    first = hygiene_scan._display("one unsafe name")
    second = hygiene_scan._display("two unsafe names")
    assert first != second
    assert hygiene_scan._display("one unsafe name") == first
    assert re.fullmatch(r"\[path [1-9][0-9]*\]", first)
