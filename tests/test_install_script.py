"""Hermetic tests for install/install.sh: dry-run only, fake system tools.

Every test runs the script with ``--dry-run`` and a PATH whose ``id``,
``getent``, ``sudo``, and ``systemctl`` are shell shims backed by a tmp
dir. ``HOUSESITTER_TEST_ROOT`` (the script's documented test-only
override) redirects the system paths there. Nothing real is changed:
the ``sudo`` shim only logs calls, and dry-run tests assert it logged
none.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "install" / "install.sh"

USER = "alice"


def write_shim(path, body):
    # type: (Path, str) -> None
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    shim = tmp_path / "shim" / "bin"
    shim.mkdir(parents=True)
    root = tmp_path / "root"
    home = tmp_path / "home" / USER
    home.mkdir(parents=True)
    sudo_log = tmp_path / "sudo.log"
    sudo_log.write_text("", encoding="utf-8")

    write_shim(
        shim / "sudo",
        'printf "%s\\n" "$*" >> "$FAKE_SUDO_LOG"\nexit 0\n',
    )
    write_shim(
        shim / "id",
        'known="${FAKE_USER:-alice}"\n'
        'if [ "$1" = "-u" ] && [ $# -eq 1 ]; then echo "${FAKE_EUID:-1000}"; exit 0; fi\n'
        'if [ "$1" = "-un" ] && [ $# -eq 1 ]; then echo "${FAKE_INVOKER:-owner}"; exit 0; fi\n'
        'if [ "$1" = "-u" ] && [ "$2" = "$known" ]; then echo "${FAKE_UID:-1000}"; exit 0; fi\n'
        'if [ "$1" = "-nG" ] && [ "$2" = "$known" ]; then\n'
        '  echo "${FAKE_GROUPS:-alice}"; exit 0\n'
        "fi\n"
        'if [ "$1" = "-gn" ] && [ "$2" = "$known" ]; then echo "$known"; exit 0; fi\n'
        "exit 1\n",
    )
    write_shim(
        shim / "getent",
        'if [ "$1" = "passwd" ] && [ "$2" = "${FAKE_USER:-alice}" ]; then\n'
        '  printf "%s:x:%s:%s::%s:/bin/bash\\n" "$2" "${FAKE_UID:-1000}" \\\n'
        '    "${FAKE_UID:-1000}" "$FAKE_HOME"\n'
        "  exit 0\n"
        "fi\n"
        "exit 2\n",
    )
    write_shim(
        shim / "systemctl",
        'printf "%s\\n" "$*" >> "$FAKE_SYSTEMCTL_LOG"\n'
        'if [ "$1" = "is-active" ]; then\n'
        '  [ "${FAKE_SYSTEMCTL_ACTIVE:-0}" = "1" ] && exit 0 || exit 3\n'
        "fi\n"
        "exit 0\n",
    )

    env = {
        "PATH": str(shim) + os.pathsep + os.environ["PATH"],
        "HOUSESITTER_TEST_ROOT": str(root),
        "FAKE_USER": USER,
        "FAKE_HOME": str(home),
        "FAKE_SUDO_LOG": str(sudo_log),
        "FAKE_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "FAKE_UID": "1000",
        "FAKE_EUID": "1000",
        "FAKE_GROUPS": USER,
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return {"root": root, "home": home, "sudo_log": sudo_log}


def run_script(*argv, stdin=""):
    # type: (*str, str) -> subprocess.CompletedProcess
    return subprocess.run(
        ["bash", str(SCRIPT), *argv],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def sudo_calls(lab):
    # type: (dict) -> str
    return lab["sudo_log"].read_text(encoding="utf-8")


def test_help_and_no_args():
    assert run_script("-h").returncode == 0
    assert "Usage:" in run_script("-h").stdout
    assert run_script("install", "--help").returncode == 0
    assert run_script().returncode != 0


def test_refuses_root(lab, monkeypatch):
    monkeypatch.setenv("FAKE_EUID", "0")
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert proc.stderr.startswith("install.sh: ")
    assert "root" in proc.stderr


@pytest.mark.parametrize("name", ["Root", "a b", "-x", "x" * 33, "root"])
def test_rejects_bad_run_as(lab, name):
    proc = run_script("install", "--run-as", name, "--dry-run")
    assert proc.returncode != 0
    assert proc.stderr.startswith("install.sh: ")


def test_rejects_unknown_user(lab, monkeypatch):
    monkeypatch.setenv("FAKE_USER", "someone-else")
    proc = run_script("install", "--run-as", "ghost", "--dry-run")
    assert proc.returncode != 0
    assert proc.stderr.startswith("install.sh: ")


def test_install_dry_run_changes_nothing(lab):
    home = lab["home"]
    proc = run_script("install", "--run-as", USER, "--with-dashboard", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "would run: sudo python3 -m venv" in out
    assert "pip install --no-cache-dir" in out
    assert USER in out  # the sudo -u account setup is planned
    assert "config.json" in out
    assert "unifi_password" in out  # the hand-made secret step is named
    assert "install-commands --exec" in out
    assert "would run: sudo install -o root -g root -m 644 <rendered unit>" in out
    assert "would run: sudo systemctl daemon-reload" in out
    assert "next step: sudo systemctl enable --now housesitter-serve" in out
    assert "next step: sudo systemctl restart musegadget" in out
    assert "warning: restarting musegadget stops any command" in out
    assert sudo_calls(lab) == ""
    assert not (lab["root"] / "opt").exists()
    assert not (home / ".config").exists()


def test_install_dry_run_restarts_musegadget_with_flag(lab):
    proc = run_script(
        "install", "--run-as", USER, "--dry-run", "--restart-musegadget"
    )
    assert proc.returncode == 0, proc.stderr
    assert "would run: sudo systemctl restart musegadget" in proc.stdout
    assert sudo_calls(lab) == ""


def test_install_refuses_existing_prefix(lab):
    (lab["root"] / "opt" / "housesitter").mkdir(parents=True)
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert "upgrade" in proc.stderr


def test_upgrade_needs_an_install(lab):
    proc = run_script("upgrade", "--dry-run")
    assert proc.returncode != 0
    assert "install --run-as" in proc.stderr


def make_installed_housesitter(prefix):
    # type: (Path) -> None
    binary = prefix / "bin" / "housesitter"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\necho "housesitter 0.1.0"\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)


def test_upgrade_dry_run(lab, monkeypatch):
    prefix = lab["root"] / "opt" / "housesitter"
    make_installed_housesitter(prefix)
    proc = run_script("upgrade", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would run: sudo" in proc.stdout
    assert "pip install --no-cache-dir --upgrade" in proc.stdout
    assert "install-commands --exec" in proc.stdout
    assert "version: housesitter 0.1.0 -> (re-run without --dry-run" in proc.stdout
    assert "next step: sudo systemctl restart musegadget" in proc.stdout
    assert sudo_calls(lab) == ""


def test_upgrade_rerenders_unit_and_restarts_only_when_active(lab, monkeypatch):
    prefix = lab["root"] / "opt" / "housesitter"
    make_installed_housesitter(prefix)
    unit = lab["root"] / "etc" / "systemd" / "system"
    unit.mkdir(parents=True)
    (unit / "housesitter-serve.service").write_text(
        "[Service]\nUser=alice\nExecStart=/opt/housesitter/bin/housesitter serve\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_SYSTEMCTL_ACTIVE", "1")
    active = run_script("upgrade", "--dry-run")
    assert active.returncode == 0, active.stderr
    assert "would run: sudo systemctl restart housesitter-serve" in active.stdout
    monkeypatch.setenv("FAKE_SYSTEMCTL_ACTIVE", "0")
    idle = run_script("upgrade", "--dry-run")
    assert idle.returncode == 0, idle.stderr
    assert "would run: sudo systemctl restart housesitter-serve" not in idle.stdout
    assert "left stopped" in idle.stdout
    assert sudo_calls(lab) == ""


def seed_uninstall_target(lab):
    prefix = lab["root"] / "opt" / "housesitter"
    make_installed_housesitter(prefix)
    unit_dir = lab["root"] / "etc" / "systemd" / "system"
    unit_dir.mkdir(parents=True)
    (unit_dir / "housesitter-serve.service").write_text(
        "[Service]\nUser=alice\n", encoding="utf-8"
    )
    dropins = lab["root"] / "etc" / "musegadget" / "commands.d"
    dropins.mkdir(parents=True)
    (dropins / "housesitter.unifi_wifi.snapshot.json").write_text("{}\n", encoding="utf-8")
    link_dir = lab["root"] / "usr" / "local" / "bin"
    link_dir.mkdir(parents=True)
    (link_dir / "housesitter").symlink_to(prefix / "bin" / "housesitter")


def test_uninstall_dry_run(lab):
    seed_uninstall_target(lab)
    root = lab["root"]
    proc = run_script("uninstall", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "would run: sudo systemctl stop housesitter-serve" in out
    assert "would run: sudo systemctl disable housesitter-serve" in out
    assert f"would run: sudo rm -f {root}/etc/systemd/system/housesitter-serve.service" in out
    assert "would run: sudo systemctl daemon-reload" in out
    assert "housesitter.unifi_wifi.snapshot.json" in out
    assert f"would run: sudo rm -f {root}/usr/local/bin/housesitter" in out
    assert f"would run: sudo rm -rf {root}/opt/housesitter" in out
    assert "reminder: delete the View Only UniFi account" in out
    assert sudo_calls(lab) == ""
    # Dry-run changed nothing: every seeded path is still there.
    assert (root / "opt" / "housesitter").is_dir()
    assert (root / "etc" / "systemd" / "system" / "housesitter-serve.service").exists()
    assert (root / "usr" / "local" / "bin" / "housesitter").is_symlink()


def test_uninstall_purge_needs_user_and_flag(lab):
    assert run_script("uninstall", "--purge", "--dry-run").returncode != 0
    assert run_script("uninstall", "--run-as", USER, "--dry-run").returncode != 0


def test_uninstall_purge_runs_as_the_run_as_account(lab):
    # Purge deletes inside the run-as account's home, which that account
    # (Muse) controls. Root must never do it: a planted symlink would
    # redirect a root rm. The account deletes its own files.
    seed_uninstall_target(lab)
    home = lab["home"]
    yes = run_script(
        "uninstall", "--purge", "--run-as", USER, "--yes", "--dry-run"
    )
    assert yes.returncode == 0, yes.stderr
    assert (
        f"would run: sudo -u {USER} -H rm -rf -- {home}/.config/housesitter "
        f"{home}/.local/share/housesitter"
    ) in yes.stdout
    assert f"sudo rm -rf {home}" not in yes.stdout
    assert sudo_calls(lab) == ""
    assert not (home / ".config").exists()


def test_uninstall_purge_dry_run_does_not_prompt(lab):
    seed_uninstall_target(lab)
    proc = run_script("uninstall", "--purge", "--run-as", USER, "--dry-run", stdin="n\n")
    assert proc.returncode == 0, proc.stderr
    assert "would ask: Remove" in proc.stdout
    assert "[y/N] " not in proc.stdout.replace("would ask: ", "")
    assert sudo_calls(lab) == ""


def test_run_as_account_in_sudo_group_keeps_its_home(lab, monkeypatch):
    # The SDK's default run-as account is the installing account, which
    # usually has sudo. The note about it must not end up in the home path.
    monkeypatch.setenv("FAKE_GROUPS", f"{USER} sudo")
    proc = run_script("install", "--run-as", USER, "--with-dashboard", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "can use sudo" in proc.stderr
    home = str(lab["home"])
    assert "-H sh -c umask 077; mkdir -p" in proc.stdout
    assert f" sh {home}\n" in proc.stdout
    assert "note:" not in proc.stdout


def test_install_never_touches_the_run_as_home_as_root(lab):
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    home = str(lab["home"])
    for line in proc.stdout.splitlines():
        if line.startswith("would run: sudo ") and home in line:
            assert line.startswith(f"would run: sudo -u {USER} -H "), line


def test_script_handles_no_secrets():
    text = SCRIPT.read_text(encoding="utf-8")
    lowered = text.lower()
    for line in lowered.splitlines():
        if "password" in line:
            assert "unifi_password" in line, line
    assert "read -s" not in lowered
    assert "--password" not in lowered
    assert "getpass" not in lowered


def test_install_warns_when_run_as_is_the_installing_account(lab, monkeypatch):
    # The SDK's default run-as account is the installing account. Then Muse
    # can change the checkout root installs from: say so, don't hide it.
    monkeypatch.setenv("FAKE_INVOKER", USER)
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "is both the run-as account" in proc.stderr
    assert sudo_calls(lab) == ""


def test_install_plans_the_checkout_write_check(lab):
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "is both the run-as account" not in proc.stderr
    assert f"would check: sudo -u {USER} test -w {ROOT}" in proc.stdout
    assert sudo_calls(lab) == ""


def test_install_builds_from_a_root_owned_copy(lab):
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "would run: sudo mktemp -d /tmp/housesitter-src.XXXXXX" in out
    assert f"pip install --no-cache-dir {ROOT}" not in out
    # Run-as home steps come before anything is installed system-wide.
    assert out.index("-H sh -c umask 077; mkdir -p") < out.index("python3 -m venv")
    assert out.index("cat > ") < out.index("python3 -m venv")


def test_home_with_percent_is_refused(lab, monkeypatch, tmp_path):
    odd = tmp_path / "home%h"
    odd.mkdir()
    monkeypatch.setenv("FAKE_HOME", str(odd))
    proc = run_script("install", "--run-as", USER, "--with-dashboard", "--dry-run")
    assert proc.returncode != 0
    assert "%" in proc.stderr
