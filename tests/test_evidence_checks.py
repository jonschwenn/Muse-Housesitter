"""Tests for scripts/evidence_checks.py (shared evidence validation, stdlib only).

Part 1 (Decision 13, option A): the publisher rebuilds the text section from
a structured results file instead of posting the PR's markdown. These tests
pin the strict schema: known keys only, types, length and count caps, and
scenario ids that exist in MAIN's scenarios list unless marked new-in-PR.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import zlib
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
        "render_error",
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


# Part 3 (S5): real PNG validation. ------------------------------------------
#
# Crafted by hand (struct + zlib), never with the repo's own encoder, so a
# bug in the encoder cannot mask a hole in the validator.


def _chunk(ctype, data):
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png(width=4, height=4, color_type=6, bit_depth=8, interlace=0, raw=None):
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    if raw is None:
        raw = b"".join(b"\x00" + bytes([9, 9, 9, 255][:channels]) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def test_valid_png_passes():
    assert evidence_checks.validate_png(_png()) == []
    assert evidence_checks.validate_png(_png(color_type=2)) == []


def test_non_png_is_rejected():
    assert evidence_checks.validate_png(b"not a png")


def test_truncated_chunk_is_rejected():
    assert evidence_checks.validate_png(_png()[:-10])


def test_bad_crc_is_rejected():
    bad = bytearray(_png())
    bad[40] ^= 0xFF  # inside the IDAT payload: the stored CRC no longer matches
    assert any("CRC" in e for e in evidence_checks.validate_png(bytes(bad)))


def test_trailing_bytes_are_rejected():
    assert any(
        "trailing" in e for e in evidence_checks.validate_png(_png() + b"junk")
    )


def test_huge_dimensions_are_rejected_without_decompressing():
    # 100000x100000 claims ~40 GB of pixels: the caps must fire first, so
    # the header is crafted directly (no pixel buffer is ever allocated).
    ihdr = struct.pack(">IIBBBBB", 100_000, 100_000, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\x00" * 100))
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data)


def test_zero_dimensions_are_rejected():
    assert evidence_checks.validate_png(_png(width=0, height=4))


def test_unsupported_colour_type_is_rejected():
    assert evidence_checks.validate_png(_png(color_type=0))
    assert evidence_checks.validate_png(_png(color_type=3))


def test_unsupported_bit_depth_or_interlace_is_rejected():
    assert evidence_checks.validate_png(_png(bit_depth=16))
    assert evidence_checks.validate_png(_png(interlace=1))


def test_missing_idat_is_rejected():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")
    assert any("IDAT" in e or "image data" in e for e in evidence_checks.validate_png(data))


def test_missing_iend_is_rejected():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([1, 2, 3, 4]) * 4 for _ in range(4))
    data = (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw))
    )
    assert evidence_checks.validate_png(data)


def test_first_chunk_must_be_ihdr():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"tEXt", b"comment")
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IEND", b"")
    )
    assert any("IHDR" in e for e in evidence_checks.validate_png(data))


def test_unknown_critical_chunk_is_rejected():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([1, 2, 3, 4]) * 4 for _ in range(4))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"ABcD", b"mystery")  # uppercase first letter: critical
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data)


def test_ancillary_chunk_is_allowed():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([1, 2, 3, 4]) * 4 for _ in range(4))
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"tEXt", b"comment")  # lowercase first letter: ancillary
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data) == []


def test_decompression_bomb_is_rejected_with_a_bound():
    # A 4x4 RGBA image holds 68 bytes of scanlines; this IDAT decodes to
    # far more. The bound is the IHDR size, so memory stays flat.
    bomb = _chunk(b"IDAT", zlib.compress(bytes(10_000)))
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + bomb + _chunk(b"IEND", b"")
    assert evidence_checks.validate_png(data)


def test_corrupt_image_data_is_rejected():
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", b"\x78\x9c\xff\xffnot-zlib")
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data)


def test_png_height_cap_matches_capture_page_cap():
    spec = importlib.util.spec_from_file_location(
        "ui_evidence_height_pin", ROOT / "scripts" / "ui_evidence.py"
    )
    ui_evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui_evidence)
    assert evidence_checks.PNG_MAX_HEIGHT == ui_evidence.MAX_PAGE_HEIGHT_PX


# Part 5 (Decision 4, option A): the CI evidence gate. ----------------------
#
# `evidence_checks.py check` runs the same refusal decision the privileged
# publisher makes, so an honest mistake turns ci-ok red before upload. (A
# hostile PR controls this copy, so the publisher's own notice, running
# main's code, still covers hostile artifacts.)


def _gate_artifact(tmp_path, results=None, png=True):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    doc = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "head_sha": "a" * 40},
        "scenarios": [
            {
                "id": "version",
                "title": "t",
                "command": ["housesitter", "--version"],
                "exit_before": 0,
                "exit_after": 0,
                "expected_exit": 0,
                "size_before": 1,
                "size_after": 1,
                "changed": False,
                "new_in_pr": False,
                "before": "$ exit 0\n",
                "after": "$ exit 0\n",
            }
        ],
        "removed": [],
    }
    if results is not None:
        doc.update(results)
    (artifact / "results.json").write_text(json.dumps(doc))
    if png:
        spec = importlib.util.spec_from_file_location(
            "ui_evidence_gate_pin", ROOT / "scripts" / "ui_evidence.py"
        )
        ui_evidence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ui_evidence)
        ui_out = artifact / "ui_out"
        ui_out.mkdir()
        shot = ui_evidence.encode_png(2, 2, 6, bytes([1, 2, 3, 4]) * 4)
        (ui_out / "typical__index-phone__head.png").write_bytes(shot)
        (ui_out / "ui_manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "captures": [
                        {
                            "fixture": "typical",
                            "view": "index-phone",
                            "status": "added",
                            "base": None,
                            "head": "typical__index-phone__head.png",
                            "diff": None,
                            "head_problems": [],
                        }
                    ],
                }
            )
        )
    return artifact


def _check(artifact, head_sha="a" * 40):
    return evidence_checks.main(
        [
            "check",
            "--artifact-dir", str(artifact),
            "--repo", "example-owner/example-repo",
            "--pr", "7",
            "--head-sha", head_sha,
        ]
    )


def test_check_passes_a_valid_artifact(tmp_path, capsys):
    assert _check(_gate_artifact(tmp_path)) == 0


def test_check_refuses_a_bad_schema(tmp_path, capsys):
    artifact = _gate_artifact(tmp_path)
    doc = json.loads((artifact / "results.json").read_text())
    doc["scenarios"][0]["id"] = "forged"
    (artifact / "results.json").write_text(json.dumps(doc))
    assert _check(artifact) == 1
    assert "bad_schema" in capsys.readouterr().out


def test_check_refuses_a_stale_head(tmp_path, capsys):
    assert _check(_gate_artifact(tmp_path), head_sha="b" * 40) == 1
    assert "stale_head" in capsys.readouterr().out


def test_check_refuses_a_missing_artifact(tmp_path, capsys):
    assert _check(tmp_path / "no-such-dir") == 1
    assert "missing" in capsys.readouterr().out


def test_check_rejects_bad_caller_arguments(tmp_path):
    artifact = _gate_artifact(tmp_path)
    assert (
        evidence_checks.main(
            ["check", "--artifact-dir", str(artifact), "--repo", "nope",
             "--pr", "7", "--head-sha", "a" * 40]
        )
        == 2
    )


def test_png_limits_are_pinned():
    # Loosening a dimension cap silently widens what the publisher accepts,
    # so the values are pinned here: changing them is a visible change to
    # this protected test file.
    assert evidence_checks.PNG_MAX_WIDTH == 4096
    assert evidence_checks.PNG_MAX_HEIGHT == 120_000
    assert evidence_checks.PNG_MAX_PIXELS == 16_777_216


# Review round 1, finding 3: the schema must require every rendered field.
#
# A scenario without `before` passed validation and then raised KeyError in
# the renderer (no refusal.json, no notice). Every field the renderer reads
# is required here with its exact type, and rendered strings must encode as
# UTF-8 (a lone surrogate encodes nowhere: it used to surface as a caller
# error at comment-write time).


@pytest.mark.parametrize(
    "key",
    [
        "id",
        "title",
        "command",
        "exit_before",
        "exit_after",
        "expected_exit",
        "size_before",
        "size_after",
        "changed",
        "before",
        "after",
    ],
)
def test_missing_rendered_field_is_rejected(key):
    entry = _scenario()
    del entry[key]
    assert evidence_checks.validate_results(_doc(entry), KNOWN_IDS) != []


def test_lone_surrogate_in_text_is_rejected():
    entry = _scenario(after="ok\ud800tail")
    assert evidence_checks.validate_results(_doc(entry), KNOWN_IDS) != []
    entry = _scenario(title="ok\ud800tail")
    assert evidence_checks.validate_results(_doc(entry), KNOWN_IDS) != []
    entry = _scenario(command=["housesitter", "ok\ud800tail"])
    assert evidence_checks.validate_results(_doc(entry), KNOWN_IDS) != []


def test_render_error_reason_is_fixed_text():
    notice = evidence_checks.refusal_notice("render_error")
    assert notice.startswith("<!-- housesitter-evidence -->\n")
    assert "`render_error`" in notice
