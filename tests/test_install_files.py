"""Tests for the install kit: example config, unit template, scan coverage."""

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path

from housesitter.core import cli as cli_mod
from housesitter.core.config import load_config
from housesitter.core.http import TlsConfig
from housesitter.core.registry import load_registry

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install"
EXAMPLE_CONFIG = INSTALL / "config.example.json"
UNIT_TEMPLATE = INSTALL / "housesitter-serve.service.in"


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


REQUIRED_DIRECTIVES = [
    "User=@RUN_AS@",
    "ExecStart=/opt/housesitter/bin/housesitter serve",
    "Restart=on-failure",
    "NoNewPrivileges=yes",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "InaccessiblePaths=-@RUN_AS_HOME@/.config/housesitter/secrets",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "RestrictNamespaces=",
    "RestrictRealtime=yes",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged @resources",
    "RestrictAddressFamilies=AF_INET AF_INET6",
    "UMask=0077",
    "ProtectProc=invisible",
    "ProcSubset=pid",
]


def render_template(user, home):
    text = UNIT_TEMPLATE.read_text(encoding="utf-8")
    text = text.replace("@RUN_AS_HOME@", home).replace("@RUN_AS@", user)
    assert "@RUN_AS" not in text
    return text


def test_unit_template_renders_and_hardens():
    rendered = render_template("muse", "/home/muse")
    assert "User=muse" in rendered
    assert "%h" not in rendered
    assert "@RUN_AS" not in rendered
    assert "Group=" not in rendered
    for directive in REQUIRED_DIRECTIVES:
        expected = directive.replace("@RUN_AS_HOME@", "/home/muse").replace(
            "@RUN_AS@", "muse"
        )
        assert expected in rendered, directive


def test_install_kit_is_visible_to_the_hygiene_scan():
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    names = out.decode().split("\0")
    for name in (
        "install/install.sh",
        "install/housesitter-serve.service.in",
        "install/config.example.json",
        "scripts/fake_unifi.py",
        "tests/test_install_files.py",
        "tests/test_install_script.py",
        "tests/test_fake_unifi.py",
    ):
        assert name in names, name


def test_example_fingerprint_is_bare_hex_for_hygiene():
    # A 32-byte colon-hex fingerprint holds runs of colon-separated hex
    # pairs that the hygiene scan flags as MACs; bare hex is clean and
    # the core accepts both forms.
    raw = json.loads(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
    pin = raw["modules"]["unifi_wifi"]["tls"]["fingerprint_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", pin) is not None
