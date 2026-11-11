"""Tests for scripts/evidence_checks.py (shared evidence validation, stdlib only).

Part 1 (Decision 13, option A): the publisher rebuilds the text section from
a structured results file instead of posting the PR's markdown. These tests
pin the strict schema: known keys only, types, length and count caps, and
scenario ids that exist in MAIN's scenarios list unless marked new-in-PR.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "evidence_checks", ROOT / "scripts" / "evidence_checks.py"
)
evidence_checks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence_checks)

KNOWN_IDS = {"version", "help"}


def _scenario(**overrides):
    entry = {
        "id": "version",
        "title": "Installed version",
        "command": ["housesitter", "--version"],
        "exit_before": 0,
        "exit_after": 0,
        "expected_exit": 0,
        "size_before": 12,
        "size_after": 12,
        "changed": False,
        "before": "$ exit 0\n--- stdout ---\nhousesitter 1.0.0\n",
        "after": "$ exit 0\n--- stdout ---\nhousesitter 1.0.0\n",
    }
    entry.update(overrides)
    return entry


def _doc(*scenarios, **overrides):
    doc = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "head_sha": "a" * 40},
        "scenarios": list(scenarios) if scenarios else [_scenario()],
    }
    doc.update(overrides)
    return doc


def test_valid_results_pass():
    assert evidence_checks.validate_results(_doc(), KNOWN_IDS) == []


def test_new_in_pr_id_passes_when_unknown():
    doc = _doc(_scenario(id="brand-new", new_in_pr=True))
    assert evidence_checks.validate_results(doc, KNOWN_IDS) == []


def test_unknown_id_without_flag_is_rejected():
    doc = _doc(_scenario(id="brand-new"))
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("brand-new" in e for e in errors)


def test_known_id_with_new_flag_is_accepted():
    # Leniency on purpose: the builder marks "new" against the base tree,
    # which can be older than main, so a known id may carry the flag.
    doc = _doc(_scenario(new_in_pr=True))
    assert evidence_checks.validate_results(doc, KNOWN_IDS) == []


def test_unknown_top_level_key_is_rejected():
    doc = _doc()
    doc["comment"] = "Approved by maintainer"
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("comment" in e for e in errors)


def test_unknown_scenario_key_is_rejected():
    doc = _doc(_scenario(note="Approved by maintainer"))
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("note" in e for e in errors)


def test_wrong_version_is_rejected():
    assert evidence_checks.validate_results(_doc(version=2), KNOWN_IDS)


def test_wrong_types_are_rejected():
    doc = _doc(_scenario(changed="yes", exit_after="0", before=123))
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert len(errors) >= 3


def test_bool_is_not_an_int_exit():
    # isinstance(True, int) is True in Python: exits must be real ints.
    doc = _doc(_scenario(exit_after=True))
    assert evidence_checks.validate_results(doc, KNOWN_IDS)


def test_timeout_exit_is_accepted():
    doc = _doc(_scenario(exit_after="timeout", after="$ exit timeout\n"))
    assert evidence_checks.validate_results(doc, KNOWN_IDS) == []


def test_oversized_text_is_rejected():
    big = "x" * (evidence_checks.RESULTS_MAX_TEXT + 1)
    doc = _doc(_scenario(after=big))
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("after" in e for e in errors)


def test_too_many_scenarios_are_rejected():
    many = [
        _scenario(id=f"s{i}", new_in_pr=True)
        for i in range(evidence_checks.RESULTS_MAX_SCENARIOS + 1)
    ]
    errors = evidence_checks.validate_results(_doc(*many), KNOWN_IDS)
    assert errors and any("scenarios" in e for e in errors)


def test_duplicate_ids_are_rejected():
    errors = evidence_checks.validate_results(_doc(_scenario(), _scenario()), KNOWN_IDS)
    assert errors and any("duplicate" in e for e in errors)


def test_bad_metadata_head_sha_is_rejected():
    doc = _doc()
    doc["metadata"] = {"head_sha": "not-a-sha"}
    assert evidence_checks.validate_results(doc, KNOWN_IDS)


def test_unknown_metadata_key_is_rejected():
    doc = _doc()
    doc["metadata"] = {"head_sha": "a" * 40, "extra": "x"}
    assert evidence_checks.validate_results(doc, KNOWN_IDS)


def test_removed_ids_must_be_known():
    doc = _doc(removed=["version"])
    assert evidence_checks.validate_results(doc, KNOWN_IDS) == []
    doc = _doc(removed=["nope"])
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("nope" in e for e in errors)


def test_id_charset_is_restricted():
    doc = _doc(_scenario(id="../evil", new_in_pr=True))
    errors = evidence_checks.validate_results(doc, KNOWN_IDS)
    assert errors and any("evil" in e for e in errors)


def test_control_characters_in_title_are_rejected():
    doc = _doc(_scenario(title="line one\nline two"))
    assert evidence_checks.validate_results(doc, KNOWN_IDS)


def test_command_must_be_a_list_of_short_strings():
    doc = _doc(_scenario(command="housesitter --version"))
    assert evidence_checks.validate_results(doc, KNOWN_IDS)
    doc = _doc(_scenario(command=["housesitter", "x" * 600]))
    assert evidence_checks.validate_results(doc, KNOWN_IDS)


def test_not_a_dict_is_rejected():
    assert evidence_checks.validate_results([], KNOWN_IDS)
    assert evidence_checks.validate_results(None, KNOWN_IDS)


def test_scenarios_must_be_a_list():
    assert evidence_checks.validate_results(_doc(scenarios={}), KNOWN_IDS)


# Part 2 (Decision 4, option B): fixed refusal notices. ----------------------
#
# When the publisher refuses an artifact it posts a notice built only from
# main's fixed text plus a reason code. No PR-supplied content may reach it.


def test_refusal_reasons_are_a_fixed_enum():
    assert set(evidence_checks.REFUSAL_REASONS) == {
        "missing",
        "too_large",
        "bad_png",
        "bad_schema",
        "stale_head",
    }


def test_refusal_notice_is_fixed_text_with_reason_code():
    for reason in evidence_checks.REFUSAL_REASONS:
        notice = evidence_checks.refusal_notice(reason)
        assert notice.startswith("<!-- housesitter-evidence -->\n")
        assert "## Before / after evidence" in notice
        assert f"`{reason}`" in notice
        # Fixed sentences only: no slots for PR content.
        assert "PWNED" not in evidence_checks.refusal_notice(reason)


def test_refusal_notice_rejects_unknown_reason():
    with pytest.raises(ValueError):
        evidence_checks.refusal_notice("pwned")


def test_refusal_notices_differ_only_by_fixed_sentence():
    bodies = {evidence_checks.refusal_notice(r) for r in evidence_checks.REFUSAL_REASONS}
    assert len(bodies) == len(evidence_checks.REFUSAL_REASONS)


def test_artifact_reason_too_large():
    assert (
        evidence_checks.artifact_refusal_reason(["shot.png is 999 bytes, over the cap"])
        == "too_large"
    )
    assert (
        evidence_checks.artifact_refusal_reason(["PNGs total 999 bytes, over the cap"])
        == "too_large"
    )


def test_artifact_reason_bad_png():
    assert (
        evidence_checks.artifact_refusal_reason(["shot.png is not a PNG file"])
        == "bad_png"
    )
    assert (
        evidence_checks.artifact_refusal_reason(["shot.png: PNG CRC mismatch"])
        == "bad_png"
    )


def test_artifact_reason_bad_schema_for_the_rest():
    assert (
        evidence_checks.artifact_refusal_reason(["manifest must be an object"])
        == "bad_schema"
    )
    assert (
        evidence_checks.artifact_refusal_reason(["manifest names a missing file: x"])
        == "bad_schema"
    )
