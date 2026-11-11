"""Hermetic installer tests with temporary homes and fake system tools.

Dry-run tests check plans without executing sudo. Partial real installs
execute only the two home setup commands, then refuse venv creation.
Upgrade stops before the pip build; uninstall executes only allow-listed
removals under the temporary system root. No real sudo or network is used.
The venv/pip build and privileged ownership changes remain untested.
"""

from __future__ import annotations

import os
import shlex
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


def test_install_dry_run_shows_commands_dir_creation(lab):
    root = lab["root"]
    commands_d = root / "etc" / "musegadget" / "commands.d"
    expected = (
        f"would create: {commands_d} "
        "(owner root, mode 755; install-commands creates it)"
    )
    missing = run_script("install", "--run-as", USER, "--dry-run")
    assert missing.returncode == 0, missing.stderr
    assert expected in missing.stdout
    assert missing.stdout.index("would create: ") < missing.stdout.index(
        "install-commands --exec"
    )
    commands_d.mkdir(parents=True)
    present = run_script("install", "--run-as", USER, "--dry-run")
    assert present.returncode == 0, present.stderr
    assert "would create: " not in present.stdout
    assert "install-commands --exec" in present.stdout


@pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "file"])
@pytest.mark.parametrize("command", ["install", "upgrade"])
def test_bad_commands_dir_refused_before_anything_installs(lab, kind, command):
    root = lab["root"]
    parent = root / "etc" / "musegadget"
    parent.mkdir(parents=True)
    path = parent / "commands.d"
    if kind == "symlink":
        real = root / "elsewhere.d"
        real.mkdir()
        path.symlink_to(real)
    elif kind == "broken-symlink":
        path.symlink_to(root / "nowhere.d")
    else:
        path.write_text("", encoding="utf-8")
    if command == "upgrade":
        (root / "opt" / "housesitter").mkdir(parents=True)
        proc = run_script("upgrade", "--dry-run")
    else:
        proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert f"{path} is not a real directory" in proc.stderr
    assert "would run:" not in proc.stdout


def _fake_sdk(root, source):
    pkg = (root / "opt" / "musegadget" / "venv" / "lib" / "python3.11"
           / "site-packages" / "musegadget")
    pkg.mkdir(parents=True)
    (pkg / "service.py").write_text(source, encoding="utf-8")


def test_install_warns_when_sdk_missing(lab):
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "Muse SDK not found in" in proc.stderr


def test_install_warns_when_sdk_predates_dropins(lab):
    _fake_sdk(lab["root"], "BUILTINS = ('system.run', 'file.write')\n")
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "predates drop-in commands" in proc.stderr
    assert "Muse SDK not found" not in proc.stderr


def test_install_quiet_when_sdk_supports_dropins(lab):
    _fake_sdk(
        lab["root"],
        'DEFAULT_COMMANDS_DIR = Path("/etc/musegadget/commands.d")\n',
    )
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "Muse SDK not found" not in proc.stderr
    assert "predates drop-in commands" not in proc.stderr


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


@pytest.mark.parametrize("command", ["install", "upgrade", "uninstall"])
def test_dry_run_plan_is_idempotent(lab, command):
    # The plan is a pure function of the checkout and the temp root:
    # a second dry run prints the same bytes and still changes nothing.
    if command == "install":
        argv = ("install", "--run-as", USER, "--with-dashboard", "--dry-run")
    elif command == "upgrade":
        make_installed_housesitter(lab["root"] / "opt" / "housesitter")
        argv = ("upgrade", "--dry-run")
    else:
        seed_uninstall_target(lab)
        argv = ("uninstall", "--dry-run")
    first = run_script(*argv)
    second = run_script(*argv)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert first.stdout == second.stdout
    assert sudo_calls(lab) == ""


@pytest.mark.parametrize(
    "argv",
    [
        ("install", "--dry-run"),  # missing --run-as
        ("install", "--run-as", USER, "--purge", "--dry-run"),
        ("install", "--run-as", USER, "--yes", "--dry-run"),
        ("install", "--run-as", USER, "--dry-run", "--bogus"),
        ("upgrade", "--run-as", USER, "--dry-run"),
        ("upgrade", "--with-dashboard", "--dry-run"),
        ("upgrade", "--purge", "--dry-run"),
        ("upgrade", "--yes", "--dry-run"),
        ("uninstall", "--with-dashboard", "--dry-run"),
        ("install", "--run-as"),  # missing value
    ],
)
def test_bad_option_sets_are_refused(lab, argv):
    proc = run_script(*argv)
    assert proc.returncode != 0
    assert proc.stderr.startswith("install.sh: ")
    assert "would run:" not in proc.stdout
    assert sudo_calls(lab) == ""


def test_unknown_command_prints_usage(lab):
    proc = run_script("frobnicate", "--dry-run")
    assert proc.returncode != 0
    assert "Usage:" in proc.stderr
    assert "would run:" not in proc.stdout
    assert sudo_calls(lab) == ""


def test_relative_test_root_is_refused(lab, monkeypatch):
    monkeypatch.setenv("HOUSESITTER_TEST_ROOT", "relative/path")
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert "HOUSESITTER_TEST_ROOT must be absolute" in proc.stderr


def test_test_root_with_whitespace_is_refused(lab, monkeypatch, tmp_path):
    monkeypatch.setenv("HOUSESITTER_TEST_ROOT", str(tmp_path / "with space"))
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert "must not hold whitespace" in proc.stderr


def test_install_without_dashboard_plans_no_unit(lab):
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "rendered unit" not in proc.stdout
    assert "housesitter-serve" not in proc.stdout
    assert "install-commands --exec" in proc.stdout
    assert sudo_calls(lab) == ""


def test_install_refuses_a_symlinked_prefix(lab):
    real = lab["root"] / "real-dir"
    real.mkdir(parents=True)
    (lab["root"] / "opt").mkdir(parents=True)
    (lab["root"] / "opt" / "housesitter").symlink_to(real)
    proc = run_script("install", "--run-as", USER, "--dry-run")
    assert proc.returncode != 0
    assert "already exists" in proc.stderr
    assert "would run:" not in proc.stdout
    assert sudo_calls(lab) == ""


def test_upgrade_dry_run_without_unit_file(lab):
    make_installed_housesitter(lab["root"] / "opt" / "housesitter")
    proc = run_script("upgrade", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "rendered unit" not in proc.stdout
    assert "housesitter-serve" not in proc.stdout
    assert "install-commands --exec" in proc.stdout
    assert "version: housesitter 0.1.0 ->" in proc.stdout
    assert sudo_calls(lab) == ""


def test_uninstall_dry_run_with_nothing_installed(lab):
    root = lab["root"]
    proc = run_script("uninstall", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert f"no unit file at {root}/etc/systemd/system/housesitter-serve.service" in out
    assert f"no housesitter drop-ins in {root}/etc/musegadget/commands.d" in out
    assert f"no {root}/usr/local/bin/housesitter; skipping" in out
    assert f"{root}/opt/housesitter is not installed; skipping" in out
    assert "would run:" not in out
    assert sudo_calls(lab) == ""


def test_uninstall_dry_run_leaves_a_foreign_symlink(lab):
    root = lab["root"]
    link_dir = root / "usr" / "local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "housesitter"
    link.symlink_to("/usr/bin/something-else")
    proc = run_script("uninstall", "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "does not point into" in proc.stdout
    assert f"would run: sudo rm -f {link}" not in proc.stdout
    assert link.is_symlink()
    assert os.readlink(link) == "/usr/bin/something-else"


@pytest.fixture
def executing_lab(lab):
    """Execute only reviewed home setup and temporary uninstall removals."""
    shim = lab["home"].parents[1] / "shim" / "bin" / "sudo"
    home_commands = (
        'umask 077; mkdir -p "$1/.config/housesitter/secrets" && chmod 700 '
        '"$1/.config/housesitter" "$1/.config/housesitter/secrets"',
        'umask 077; f="$1/.config/housesitter/config.json"; '
        'if [ -e "$f" ] || [ -L "$f" ]; then exit 0; fi; set -C; cat > "$f"',
    )
    allowed = "|".join(shlex.quote(command) for command in home_commands)
    write_shim(
        shim,
        'printf "%s\\n" "$*" >> "$FAKE_SUDO_LOG"\n'
        'if [ "$#" -eq 8 ] && [ "$1" = "-u" ] && [ "$2" = "$FAKE_USER" ] '
        '&& [ "$3" = "-H" ] && [ "$4" = "sh" ] && [ "$5" = "-c" ] '
        '&& [ "$7" = "sh" ] && [ "$8" = "$FAKE_HOME" ]; then\n'
        f'  case "$6" in {allowed}) shift 3; exec "$@" ;; esac\n'
        'fi\n'
        'if [ "$#" -eq 3 ] && [ "$1" = "rm" ]; then\n'
        '  case "$2:$3" in\n'
        '    "-rf:$HOUSESITTER_TEST_ROOT/opt/housesitter"|'
        '"-f:$HOUSESITTER_TEST_ROOT/usr/local/bin/housesitter"|'
        '"-f:$HOUSESITTER_TEST_ROOT/etc/musegadget/commands.d/housesitter.example.json") '
        'exec /bin/rm "$2" "$3" ;;\n'
        '  esac\n'
        'fi\n'
        'echo "test shim refused privileged build or unexpected command" >&2\n'
        'exit 97\n',
    )
    return lab


@pytest.mark.parametrize("existing", [False, True])
def test_real_install_sets_private_home_and_preserves_config(executing_lab, existing):
    lab = executing_lab
    config_dir = lab["home"] / ".config/housesitter"
    config = config_dir / "config.json"
    original = b'{"owner_setting": true}\n'
    if existing:
        config_dir.mkdir(parents=True)
        config.write_bytes(original)
        config.chmod(0o600)
    proc = run_script("install", "--run-as", USER)
    assert proc.returncode == 1, proc.stderr
    assert "could not create the venv" in proc.stderr
    assert config.read_bytes() == (original if existing else
                                   (ROOT / "install/config.example.json").read_bytes())
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    for directory in (config_dir, config_dir / "secrets"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert not (lab["root"] / "opt/housesitter").exists()
    calls = sudo_calls(lab).splitlines()
    assert len(calls) == 4  # trust check, two home commands, refused venv
    assert all(call.startswith(f"-u {USER} -H sh -c ") for call in calls[1:3])
    assert calls[-1] == f"python3 -m venv {lab['root']}/opt/housesitter"


@pytest.mark.parametrize("command", ["upgrade", "uninstall"])
def test_real_upgrade_or_uninstall_keeps_owner_files(executing_lab, command):
    lab = executing_lab
    files = {}
    for relative in (".config/housesitter/config.json",
                     ".config/housesitter/secrets/unifi_password",
                     ".local/share/housesitter/snapshots/example.json"):
        path = lab["home"] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PLANTED-SECRET-owner-file\n")
        path.chmod(0o600)
        files[path] = (path.read_bytes(), path.stat().st_mode)
    prefix = lab["root"] / "opt/housesitter"
    prefix.mkdir(parents=True)
    (prefix / "installed-marker").write_text("installed", encoding="utf-8")
    link = lab["root"] / "usr/local/bin/housesitter"
    link.parent.mkdir(parents=True)
    link.symlink_to(prefix / "bin/housesitter")
    dropin = lab["root"] / "etc/musegadget/commands.d/housesitter.example.json"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("{}", encoding="utf-8")
    proc = run_script(command)
    if command == "upgrade":
        assert proc.returncode == 1, proc.stderr
        assert "could not upgrade Housesitter" in proc.stderr
        assert "mktemp -d /tmp/housesitter-src." in sudo_calls(lab)
        assert prefix.is_dir() and link.is_symlink() and dropin.is_file()
    else:
        assert proc.returncode == 0, proc.stderr
        assert not prefix.exists()
        assert not link.is_symlink()
        assert not dropin.exists()
    for path, (content, mode) in files.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode == mode
