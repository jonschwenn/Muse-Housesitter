"""Tests for core/secrets.py: modes, ownership, names, and silence."""

from __future__ import annotations

import os

import pytest

from housesitter.core.secrets import SecretError, read_secret

VALUE = "PLANTED-SECRET-unifi-password-1"


def write_secret(path, value=VALUE, mode=0o600):
    path.write_text(value + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_round_trip_strips_one_trailing_newline(tmp_path):
    write_secret(tmp_path / "unifi_password")
    assert read_secret("unifi_password", tmp_path) == VALUE
    assert "\n" not in read_secret("unifi_password", tmp_path)


def test_owner_only_mode_400_is_accepted(tmp_path):
    write_secret(tmp_path / "unifi_password", mode=0o400)
    assert read_secret("unifi_password", tmp_path) == VALUE


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o666, 0o610])
def test_group_or_world_readable_is_refused(tmp_path, mode):
    write_secret(tmp_path / "unifi_password", mode=mode)
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)


def test_not_owned_by_current_user_is_refused(tmp_path, monkeypatch):
    write_secret(tmp_path / "unifi_password")
    other = 0 if os.getuid() != 0 else 1
    monkeypatch.setattr(os, "getuid", lambda: other)
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)


@pytest.mark.parametrize("name", ["../escape", "sub/dir", "", ".hidden", "a" * 65])
def test_bad_names_are_refused(tmp_path, name):
    with pytest.raises(SecretError):
        read_secret(name, tmp_path)


@pytest.mark.parametrize("content", ["", "\n", "first\nsecond\n"])
def test_empty_or_multiline_file_is_refused(tmp_path, content):
    target = tmp_path / "unifi_password"
    target.write_text(content, encoding="utf-8")
    os.chmod(target, 0o600)
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)


def test_error_text_never_contains_the_secret(tmp_path):
    target = tmp_path / "unifi_password"
    target.write_text(VALUE + "\n", encoding="utf-8")
    os.chmod(target, 0o644)
    with pytest.raises(SecretError) as excinfo:
        read_secret("unifi_password", tmp_path)
    assert VALUE not in str(excinfo.value)


def test_error_text_for_missing_file_has_no_value(tmp_path):
    with pytest.raises(SecretError) as excinfo:
        read_secret("unifi_password", tmp_path)
    assert "unifi_password" in str(excinfo.value)


def test_symlink_is_refused_even_to_own_file(tmp_path):
    target = tmp_path / "unifi_password"
    write_secret(target)
    link = tmp_path / "link_to_own"
    link.symlink_to(target)
    with pytest.raises(SecretError):
        read_secret("link_to_own", tmp_path)


def test_symlink_to_directory_is_refused(tmp_path):
    subdir = tmp_path / "asubdir"
    subdir.mkdir()
    link = tmp_path / "link_to_dir"
    link.symlink_to(subdir)
    with pytest.raises(SecretError):
        read_secret("link_to_dir", tmp_path)


def test_dangling_symlink_is_refused(tmp_path):
    link = tmp_path / "dangling"
    link.symlink_to(tmp_path / "no_such")
    with pytest.raises(SecretError):
        read_secret("dangling", tmp_path)


def test_fifo_is_refused_without_hanging(tmp_path):
    import os as _os

    fifo = tmp_path / "myfifo"
    _os.mkfifo(fifo)
    _os.chmod(fifo, 0o600)
    with pytest.raises(SecretError):
        read_secret("myfifo", tmp_path)


def test_oversize_file_is_refused(tmp_path):
    import os as _os

    from housesitter.core import secrets as secrets_mod

    target = tmp_path / "unifi_password"
    target.write_bytes(b"A" * (secrets_mod.MAX_SECRET_BYTES + 1) + b"\n")
    _os.chmod(target, 0o600)
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)


def test_file_exactly_at_the_cap_is_accepted(tmp_path):
    import os as _os

    from housesitter.core import secrets as secrets_mod

    target = tmp_path / "unifi_password"
    target.write_bytes(b"A" * (secrets_mod.MAX_SECRET_BYTES - 1) + b"\n")
    _os.chmod(target, 0o600)
    assert read_secret("unifi_password", tmp_path) == "A" * (secrets_mod.MAX_SECRET_BYTES - 1)


def test_stat_to_open_swap_cannot_change_the_value(tmp_path, monkeypatch):
    import os as _os
    import pathlib

    target = tmp_path / "racer"
    write_secret(target, value=VALUE)
    hostile = tmp_path / "hostile_src"
    write_secret(hostile, value="PLANTED-SECRET-swapped-6")
    real_stat = pathlib.Path.stat

    def evil_stat(self):
        result = real_stat(self)
        if str(self).endswith("racer"):
            _os.rename(str(self), str(self) + ".orig")
            _os.symlink(str(hostile), str(self))
        return result

    monkeypatch.setattr(pathlib.Path, "stat", evil_stat)
    assert read_secret("racer", tmp_path) == VALUE


def test_exactly_one_trailing_newline_is_stripped(tmp_path):
    import os as _os

    cases = [
        ("lf", VALUE + "\n", VALUE),
        ("crlf", VALUE + "\r\n", VALUE),
    ]
    for label, content, expected in cases:
        name = "e_" + label
        path = tmp_path / name
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        _os.chmod(path, 0o600)
        assert read_secret(name, tmp_path) == expected


@pytest.mark.parametrize("content", [
    VALUE + "\n\n",
    VALUE + "\r\n\r\n",
    VALUE + "\r",
    "abc\rdef\n",
])
def test_extra_trailing_newlines_and_lone_cr_are_refused(tmp_path, content):
    import os as _os

    target = tmp_path / "unifi_password"
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)
    _os.chmod(target, 0o600)
    with pytest.raises(SecretError):
        read_secret("unifi_password", tmp_path)
