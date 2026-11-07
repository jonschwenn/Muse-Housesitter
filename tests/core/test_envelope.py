"""Tests for core/envelope.py: envelope shape, budget, sanitiser."""

from __future__ import annotations

import json

import pytest

from housesitter.core.envelope import (
    BUDGET_BYTES,
    build_error,
    build_ok,
    format_error,
    measure,
    sanitize_label,
    sanitize_text,
    serialize,
    within_budget,
)

SECRET = "PLANTED-SECRET-hostile-device-1"


def make_ok(**overrides):
    fields = {
        "schema": "housesitter.example.snapshot/1",
        "module": "example",
        "module_version": "0.1.0",
        "housesitter_version": "0.1.0",
        "collected_at": "2026-11-04T15:15:00Z",
        "source": {"kind": "example"},
        "data": {"aps": []},
    }
    fields.update(overrides)
    return build_ok(**fields)


def test_success_envelope_has_the_fixed_key_set():
    env = make_ok()
    assert set(env) == {
        "ok", "schema", "module", "module_version", "housesitter_version",
        "collected_at", "source", "data", "warnings", "truncated", "saved", "more",
    }
    assert env["ok"] is True
    assert env["warnings"] == [] and env["truncated"] is False
    assert env["saved"] is None and env["more"] is None


def test_label_is_present_only_when_given():
    assert "label" not in make_ok()
    assert make_ok(label="before")["label"] == "before"


def test_error_object_shape():
    assert build_error("upstream_auth", "login rejected") == {
        "ok": False,
        "error": {"code": "upstream_auth", "message": "login rejected"},
    }


def test_error_line_format_and_codes():
    line = format_error("unifi_wifi", "upstream_auth", "login rejected")
    assert line == "housesitter: unifi_wifi: upstream_auth: login rejected"
    with pytest.raises(ValueError):
        format_error("unifi_wifi", "no_such_code", "x")
    with pytest.raises(ValueError):
        format_error("Bad Module!", "timeout", "x")


def test_error_line_is_capped():
    line = format_error("unifi_wifi", "timeout", "x" * 1000)
    assert len(line) < 400
    assert line.startswith("housesitter: unifi_wifi: timeout: ")


def test_serialize_is_compact_and_measures_with_the_sdk_formula():
    text = serialize(make_ok(data={"a": [1, 2]}))
    assert ": " not in text and ", " not in text
    assert measure(text) == len(json.dumps(text)) - 2


def test_budget_boundary_just_under_exactly_at_just_over():
    assert BUDGET_BYTES == 88 * 1024
    assert within_budget("x" * (BUDGET_BYTES - 1))
    assert within_budget("x" * BUDGET_BYTES)
    assert not within_budget("x" * (BUDGET_BYTES + 1))


def test_budget_counts_escaping_like_the_sdk():
    # Every non-ASCII char becomes a 6-char \uXXXX escape under json.dumps.
    text = "\u00e9" * 1000
    assert measure(text) == len(json.dumps(text)) - 2
    assert measure(text) > len(text.encode("utf-8"))


def test_sanitiser_strips_hostile_characters():
    assert sanitize_text("a\u202eb\u200bc\x00d\x01e") == "abcde"
    assert sanitize_text("a\tb\nc  d") == "a b c d"
    assert sanitize_text("  padded  ") == "padded"
    assert sanitize_text(None) is None
    assert sanitize_text(7) == 7


def test_sanitiser_caps_at_64_and_labels_at_200():
    assert sanitize_text("y" * 5000) == "y" * 64
    assert sanitize_label("z" * 5000) == "z" * 200


def test_envelope_keys_never_come_from_untrusted_text():
    # Modules sanitise before placing values in data; the envelope then
    # keeps its fixed key set and never promotes a value to a key.
    hostile = "x\u202ey" * 40
    env = make_ok(data={"clients": [{"name": sanitize_text(hostile)}]})
    assert hostile not in serialize(env)
    assert set(env) == set(make_ok())
    assert set(env["data"]) == {"clients"}


def test_planted_secret_never_appears_in_error_paths():
    reason = "upstream returned an error page"
    line = format_error("example", "upstream_unexpected", reason)
    assert SECRET not in line
    assert SECRET not in serialize(build_error("upstream_unexpected", reason))


@pytest.mark.parametrize("reason,expected", [
    ("first\nsecond", "first second"),
    ("a\rb", "a b"),
    ("a\r\nb", "a b"),
    ("a\x00b", "a b"),
    ("a\t\tb", "a b"),
    ("a\x01\x7fb", "a b"),
])
def test_format_error_collapses_whitespace_and_controls(reason, expected):
    line = format_error("example", "timeout", reason)
    assert "\n" not in line and "\r" not in line and "\x00" not in line
    assert line == "housesitter: example: timeout: " + expected


def test_format_error_keeps_single_line():
    line = format_error("unifi_wifi", "timeout", "first\nsecond")
    assert line.count("\n") == 0


def test_sanitiser_strips_lone_surrogates():
    import unicodedata

    assert sanitize_text("a\ud800b") == "ab"
    assert sanitize_text("\udc00") == ""
    out = sanitize_text("a\ud800b")
    assert "Cs" not in {unicodedata.category(char) for char in out}
