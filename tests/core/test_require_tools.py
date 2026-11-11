"""Fail-closed handling of missing test prerequisites.

When ``HOUSESITTER_REQUIRE_TEST_TOOLS=1`` (set for the CI ``test`` job),
a missing prerequisite (``openssl``, wheel-build tooling) must fail the
test instead of skipping it, so a broken build or a stripped runner can
never show up as green. With the variable unset, the old skip behaviour
stays, so local runs on machines without the tooling still pass.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from . import helpers
from .helpers import fail_on_proc_error, require_test_tool, tools_required

VAR = "HOUSESITTER_REQUIRE_TEST_TOOLS"


def test_tools_required_unset_by_default(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert tools_required() is False


def test_tools_required_set_to_1(monkeypatch):
    monkeypatch.setenv(VAR, "1")
    assert tools_required() is True


@pytest.mark.parametrize("value", ["0", "yes", "", "true"])
def test_tools_required_ignores_other_values(monkeypatch, value):
    monkeypatch.setenv(VAR, value)
    assert tools_required() is False


def test_require_present_tool_passes_through():
    assert require_test_tool(True, "anything") is None


def test_require_missing_tool_skips_by_default(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    with pytest.raises(pytest.skip.Exception, match="openssl"):
        require_test_tool(False, "openssl is not on PATH")


def test_require_missing_tool_fails_when_required(monkeypatch):
    monkeypatch.setenv(VAR, "1")
    with pytest.raises(pytest.fail.Exception, match="openssl"):
        require_test_tool(False, "openssl is not on PATH")


def test_generate_cert_missing_openssl_skips_by_default(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.skip.Exception, match="openssl"):
        helpers.generate_cert(tmp_path)


def test_generate_cert_missing_openssl_fails_when_required(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(VAR, "1")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(pytest.fail.Exception, match="openssl"):
        helpers.generate_cert(tmp_path)


def test_fail_on_proc_error_passes_on_success():
    proc = subprocess.CompletedProcess(
        args=["pip", "wheel"], returncode=0, stdout=b"", stderr=b""
    )
    assert fail_on_proc_error(proc, "wheel build") is proc


def test_fail_on_proc_error_reports_stderr_tail():
    marker = b"MARKER-never-missing-12345"
    proc = subprocess.CompletedProcess(
        args=["pip", "wheel"], returncode=1,
        stdout=b"", stderr=b"x" * 500 + marker,
    )
    with pytest.raises(pytest.fail.Exception) as excinfo:
        fail_on_proc_error(proc, "wheel build")
    message = str(excinfo.value)
    assert "wheel build" in message
    assert "rc=1" in message
    assert marker.decode() in message


def test_fail_on_proc_error_accepts_called_process_error():
    exc = subprocess.CalledProcessError(1, ["pip", "install"], stderr=b"boom")
    with pytest.raises(pytest.fail.Exception, match="boom"):
        fail_on_proc_error(exc, "fresh-venv install")
