"""Tests for the install kit: example config and dashboard unit properties.

The example config must load and pass ``config-check``. That check builds
a ``TlsConfig`` from the example fingerprint pin, which is what covers the
pin staying valid; the hygiene scanner never flagged colon-hex
fingerprints, so no separate pin-format test is needed. The install kit
needs no visibility test either: the local hygiene suite
(``tests/test_hygiene.py``) and CI's trusted scan read the whole tree, not
a file listing.

The dashboard unit template is tested through the properties that matter
(D25/D27), parsed from the rendered unit rather than matched line by
line: the unprivileged run-as user, only the read-only server as a
long-running process, the sandboxing that enforces read-only and no new
privileges, the absolute installed entry point, and no timer or polling
(hard rule 3). ``systemd-analyze verify`` checks the rendered unit
against a real systemd when one is installed.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core.helpers import require_test_tool
from housesitter.core import cli as cli_mod
from housesitter.core.config import load_config
from housesitter.core.http import TlsConfig
from housesitter.core.registry import load_registry

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
EXAMPLE_CONFIG = INSTALL / "config.example.json"
UNIT_TEMPLATE = INSTALL / "housesitter-serve.service.in"
INSTALL_SCRIPT = INSTALL / "install.sh"

systemd_analyze = shutil.which("systemd-analyze")


def test_example_config_passes_config_check(tmp_path):
    registry = load_registry()
    config = load_config(registry, EXAMPLE_CONFIG)
    module = config.module("unifi_wifi")
    assert module.enabled is True
    assert module.settings["base_url"] == "https://192.0.2.1"
    assert module.settings["site"] == "default"
    assert module.settings["flavor"] == "unifi_os"
    assert module.settings["auth"] == {
        "username": "housesitter-readonly",
        "password_secret": "unifi_password",
    }
    assert module.settings["request_timeout_s"] == 15
    assert config.identifiers == "full"
    assert config.retention == 500
    assert config.dashboard.bind == "127.0.0.1"
    assert config.dashboard.port == 8080
    # The pinned fingerprint must be a real 32-byte pin the core accepts.
    TlsConfig(
        fingerprint_sha256=module.settings["tls"]["fingerprint_sha256"]
    )
    out, err = io.StringIO(), io.StringIO()
    code = cli_mod.main(
        ["config-check"],
        env={"HOME": str(tmp_path)},
        stdout=out,
        stderr=err,
        _exit=lambda _code: None,
        registry=registry,
        config=config,
    )
    assert code == 0
    assert "config ok:" in out.getvalue()
    assert "unifi_wifi: enabled" in out.getvalue()


def parse_directives(text):
    # type: (str) -> list
    """(section, key, value) for every directive line in a unit file."""
    triples = []
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        key, sep, value = stripped.partition("=")
        assert sep, line
        triples.append((section, key.strip(), value.strip()))
    return triples


def rendered_unit(user="muse", home="/home/muse"):
    # type: (str, str) -> tuple
    """(text, service dict, triples) for the template rendered for a user."""
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    assert "@RUN_AS_HOME@" in text and "@RUN_AS@" in text
    # Home first: "@RUN_AS@" is a prefix of "@RUN_AS_HOME@".
    rendered = text.replace("@RUN_AS_HOME@", home).replace("@RUN_AS@", user)
    assert "@RUN_AS" not in rendered
    assert "%" not in rendered
    triples = parse_directives(rendered)
    service = {}
    for section, key, value in triples:
        if section == "Service":
            service[key] = value
    return rendered, service, triples


def test_unit_runs_as_the_intended_unprivileged_user():
    for user, home in (("muse", "/home/muse"), ("alice", "/home/alice")):
        _, service, _ = rendered_unit(user, home)
        assert service["User"] == user
        assert service["User"] not in ("root", "0")
        assert "Group" not in service or service["Group"] not in ("root", "0")


def test_unit_runs_only_the_dashboard_server():
    # The read-only dashboard server is the only long-running process:
    # exactly one entry point, a simple (long-running) service that
    # restarts only on crash, with nothing else run before or around it.
    _, service, triples = rendered_unit()
    starts = [v for s, k, v in triples if s == "Service" and k == "ExecStart"]
    assert len(starts) == 1
    argv = starts[0].split()
    assert argv[0] == "/opt/housesitter/bin/housesitter"
    assert argv[0].startswith("/")
    assert argv[1:] == ["serve"]
    assert service["Type"] == "simple"
    assert service["Restart"] == "on-failure"
    assert "RestartSec" in service
    assert "ExecStartPre" not in service
    assert "ExecCondition" not in service


# Sandboxing directives whose exact values carry the read-only and
# no-new-privileges properties (D25): an empty value means the directive
# must be present but cleared, resetting the default.
PINNED_SANDBOX = {
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "read-only",
    "UMask": "0077",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
}

# Further hardening that must stay present; values may be tuned without
# changing what the service promises.
HARDENING_DIRECTIVES = [
    "ProtectKernelTunables",
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectControlGroups",
    "ProtectClock",
    "ProtectHostname",
    "ProtectProc",
    "ProcSubset",
    "RestrictNamespaces",
    "RestrictRealtime",
    "RestrictSUIDSGID",
    "LockPersonality",
    "RemoveIPC",
    "MemoryDenyWriteExecute",
    "SystemCallArchitectures",
    "SystemCallFilter",
    "RestrictAddressFamilies",
]


def test_unit_denies_privilege_and_writes():
    _, service, triples = rendered_unit(home="/home/muse")
    for key, value in PINNED_SANDBOX.items():
        assert service[key] == value, key
    # The UniFi secret directory stays hidden from the server.
    hidden = service["InaccessiblePaths"].lstrip("-")
    assert hidden == "/home/muse/.config/housesitter/secrets", hidden
    for key in HARDENING_DIRECTIVES:
        assert key in service, key
    # The service set minus privileged and resource-control calls.
    filters = sorted(v for s, k, v in triples if s == "Service" and k == "SystemCallFilter")
    assert filters == ["@system-service", "~@privileged @resources"]


TIMER_LIKE = re.compile(
    r"timer|calendar|OnActive|OnBoot|OnStartup|cron|poll|wake|alarm",
    re.IGNORECASE,
)


def test_unit_enables_no_timer_or_polling():
    # Hard rule 3: nothing schedules collection. The unit has no timer
    # section, no schedule-like directive, and starts at boot only when
    # the owner enables it.
    _, service, triples = rendered_unit()
    assert {s for s, _, _ in triples} == {"Unit", "Service", "Install"}
    for _, key, _ in triples:
        assert TIMER_LIKE.search(key) is None, key
    install = {k: v for s, k, v in triples if s == "Install"}
    assert install["WantedBy"] == "multi-user.target"
    script = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"\b(timer|cron)\b", script, re.IGNORECASE) is None


def test_rendered_unit_passes_systemd_analyze_verify(tmp_path):
    # Both CI runner images document an installed systemd (255 on 24.04,
    # 259 on the newest image), and the binary ships in the systemd
    # package itself, so a missing binary fails under
    # HOUSESITTER_REQUIRE_TEST_TOOLS and skips otherwise.
    require_test_tool(systemd_analyze is not None, "systemd-analyze is not installed")
    rendered, _, _ = rendered_unit()
    # The installed entry point does not exist on test machines, so point
    # the copy at this interpreter for the syntax check. The real path is
    # pinned by test_unit_runs_only_the_dashboard_server.
    stubbed = re.sub(
        r"^ExecStart=\S+",
        "ExecStart=" + sys.executable,
        rendered,
        flags=re.MULTILINE,
    )
    assert stubbed != rendered
    unit = tmp_path / "housesitter-serve.service"
    unit.write_text(stubbed, encoding="utf-8")
    proc = subprocess.run(
        [systemd_analyze, "verify", str(unit)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
