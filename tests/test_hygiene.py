"""Repo hygiene scan (AGENTS.md, Testing rules).

Fails if any file git would include (tracked, or new and not ignored) contains
data that looks like it came from a real house, or a likely secret. Fixtures
must use invented values:

- MACs: 02:00:00:xx:xx:xx (locally administered) or 00:00:5e:00:53:xx (RFC 7042)
- IPv4: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24 (RFC 5737), plus
  loopback and 0.0.0.0
- SSIDs in fixtures: end in "-example"
- Planted secrets in hostile fixtures: contain "PLANTED-SECRET"

Patterns are assembled from parts so that this file does not match itself.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

HEX = "[0-9A-Fa-f]"
MAC_RES = [
    # 02:00:00:00:00:01 and 02-00-00-00-00-01
    re.compile(rf"(?<![0-9A-Fa-f:])(?:{HEX}{{2}}[:-]){{5}}{HEX}{{2}}(?![0-9A-Fa-f:])"),
    # 0200.0000.0001 (Cisco style)
    re.compile(rf"(?<![0-9A-Fa-f.])(?:{HEX}{{4}}\.){{2}}{HEX}{{4}}(?![0-9A-Fa-f.])"),
]
# 020000000001: bare, as some upstream APIs return it. Checked in fixtures only,
# because elsewhere 12 hex digits are usually a short git SHA or a counter.
BARE_MAC_RE = re.compile(rf"(?<![0-9A-Fa-f-])(?:{HEX}{{12}})(?![0-9A-Fa-f-])")
ALLOWED_MAC_PREFIXES = ("020000", "00005e0053")

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
# The lookahead excludes "." so an IPv4-mapped address (::ffff:192.0.2.1) is left
# to the IPv4 check instead of being cut at the dot.
IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:.])"
)
ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "192.0.2.0/24",  # RFC 5737 documentation ranges
        "198.51.100.0/24",
        "203.0.113.0/24",
        "127.0.0.0/8",  # loopback
        "0.0.0.0/32",
        "255.0.0.0/8",  # netmasks and broadcast, e.g. 255.255.255.0
        "2001:db8::/32",  # RFC 3849 IPv6 documentation range
        "::1/128",
        "ff00::/8",  # multicast, e.g. ff02::1
    )
]

# A line containing this marker is skipped, for the rare false positive (e.g. a
# four-part version number that looks like an IP). Reviewers check every use.
SUPPRESS = "hygiene: ok"

PLANTED = "PLANTED-SECRET"
# The key must END in one of these words: client_secret, UNIFI_PASSWORD, and
# ssh_key match; tokenizer, secret_id, token_file and compass_heading don't.
# Any UniFi-style x_ field (x_authkey, x_vwirekey, x_passphrase) counts too.
KEY_WORDS = (
    r"(?:x_\w+|\w*(?:password|passwd|passphrase|secret|token|api[_-]?key|auth[_-]?key|_key))"
)
SECRET_PATTERNS = [
    ("private key", re.compile("-----BEGIN [A-Z ]*" + "PRIVATE " + "KEY-----")),
    ("Muse SDK token", re.compile("mg" + r"st_[A-Za-z0-9_-]{16,}")),
    ("GitHub token", re.compile("gh" + r"[pousr]_[A-Za-z0-9]{30,}")),
    ("AWS access key", re.compile("AK" + r"IA[0-9A-Z]{16}")),
    ("Slack token", re.compile("xo" + r"x[baprs]-[A-Za-z0-9-]{10,}")),
    ("credentials in a URL", re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@")),
    ("JSON web token", re.compile("ey" + r"J[A-Za-z0-9_-]{8,}\.ey" + r"J[A-Za-z0-9_-]{8,}")),
    ("bearer token", re.compile(r"(?i)\bbearer\s+(?=[\w.~+/-]*\d)[\w.~+/-]{16,}")),
    (
        "session cookie",
        re.compile(r"(?i)\b(?:unifises|token|csrf_token|session(?:id)?)=(?=[^;\s\"']*\d)[^;\s\"']{8,}"),
    ),
    (
        "CSRF token value",
        re.compile(
            r"(?i)x-(?:updated-)?csrf-token[\"']?\s*:\s*[\"']?(?=[\w-]*\d)[\w-]{16,}"
        ),
    ),
    (
        "credential assignment",
        # Key, then : or =, then a quoted value of 8+ characters.
        re.compile(rf"""(?i)["']?{KEY_WORDS}["']?\s*[:=]\s*(?:"([^"\n]{{8,}})"|'([^'\n]{{8,}})')"""),
    ),
]
# YAML, INI, and env files also allow unquoted values. Not applied to Python,
# where an unquoted value is code (password = ctx.secret("unifi_password")).
UNQUOTED_ASSIGNMENT = re.compile(
    rf"""(?im)^[ \t]*(?:export[ \t]+)?["']?{KEY_WORDS}["']?[ \t]*[:=][ \t]*"""
    r"""([^\s"'#,{}\[\]][^\s#,]{7,})"""
)
UNQUOTED_EXEMPT_SUFFIXES = {".py"}
# Values that name or reference a secret rather than contain one.
REFERENCE_VALUE = re.compile(
    r"^(?:\$\{\{.*\}\}"  # ${{ github.token }}
    r"|\$\{?\w+\}?"  # $VAR or ${VAR}
    r"|<[^>]+>"  # <placeholder>
    r"|[a-z][a-z0-9]*(?:_[a-z0-9]+)+"  # snake_case name of a secret file, e.g. unifi_password
    r"|[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+"  # a header name, e.g. X-CSRF-Token
    r"|(?:~|\.{1,2})?/\S*"  # a path, e.g. ~/.config/housesitter/secrets/unifi
    r"|(?i:changeme|change-me|example|redacted|placeholder|x{4,}|\*{4,}))$"  # placeholders
)
SSID_KEYS = {"ssid", "essid"}


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


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def mac_problems(text: str, bare: bool = False) -> list[str]:
    found = []
    for pattern in MAC_RES + ([BARE_MAC_RE] if bare else []):
        for match in pattern.finditer(text):
            digits = re.sub("[^0-9a-f]", "", match.group(0).lower())
            if pattern is BARE_MAC_RE and digits.isdigit():
                continue  # a 12-digit number, e.g. a byte counter
            if not digits.startswith(ALLOWED_MAC_PREFIXES):
                found.append(f"MAC {match.group(0)}")
    return found


def ip_problems(text: str) -> list[str]:
    found = []
    for match in IPV4_RE.finditer(text):
        parts = [int(p) for p in match.group(0).split(".")]
        if any(p > 255 for p in parts):
            continue  # e.g. a version like 6.6.77.15402
        addr = ipaddress.ip_address(".".join(map(str, parts)))  # tolerates leading zeros
        if not any(addr in net for net in ALLOWED_NETS):
            found.append(f"IPv4 {match.group(0)}")
    for match in IPV6_RE.finditer(text):
        candidate = match.group(0)
        if sum(1 for group in candidate.split(":") if group) < 2:
            continue  # e.g. Python slices like [::2]
        try:
            addr = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if not any(addr in net for net in ALLOWED_NETS):
            found.append(f"IPv6 {candidate}")
    return found


def secret_problems(text: str, unquoted: bool = True) -> list[str]:
    found = []
    patterns = SECRET_PATTERNS + (
        [("credential assignment", UNQUOTED_ASSIGNMENT)] if unquoted else []
    )
    for label, pattern in patterns:
        for match in pattern.finditer(text):
            if PLANTED in match.group(0):
                continue
            value = next((g for g in match.groups() if g), None) if match.groups() else None
            if value is not None and REFERENCE_VALUE.match(value.strip()):
                continue
            found.append(f"likely secret ({label})")
    return found


def ssid_problems(obj: object) -> list[str]:
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in SSID_KEYS and isinstance(value, str) and value:
                if not value.endswith("-example"):
                    found.append(f"SSID {value!r} does not end in -example")
            found.extend(ssid_problems(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(ssid_problems(item))
    return found


def is_fixture(path: Path) -> bool:
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        return False
    return rel.startswith("tests/fixtures/") and path.suffix == ".json"


def scan(path: Path) -> list[str]:
    text = read_text(path)
    if text is None:
        return []
    lines = [line for line in text.splitlines() if SUPPRESS not in line]
    checked = "\n".join(lines)
    problems = (
        mac_problems(checked, bare=is_fixture(path))
        + ip_problems(checked)
        + secret_problems(checked, unquoted=path.suffix not in UNQUOTED_EXEMPT_SUFFIXES)
    )
    if is_fixture(path):
        try:
            problems += ssid_problems(json.loads(text))
        except ValueError:
            problems.append("fixture is not valid JSON")
    return problems


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
