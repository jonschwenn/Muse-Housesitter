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
import subprocess
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
        "bad_skip",
        "stale_head",
        "render_error",
        "publish_failed",
    }


def test_publish_failed_notice_is_fixed_text():
    notice = evidence_checks.refusal_notice("publish_failed")
    assert notice.startswith("<!-- housesitter-evidence -->\n")
    assert "`publish_failed`" in notice


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


def _png_chunks(extra_before_idat=(), extra_after_idat=(), color_type=6):
    """A small PNG with extra chunks around the single IDAT."""
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    raw = b"".join(
        b"\x00" + bytes([1, 2, 3, 4])[:channels] * 4 for _ in range(4)
    )
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + b"".join(_chunk(t, d) for t, d in extra_before_idat)
        + _chunk(b"IDAT", zlib.compress(raw))
        + b"".join(_chunk(t, d) for t, d in extra_after_idat)
        + _chunk(b"IEND", b"")
    )


def test_allow_listed_chunks_pass():
    # Only IHDR/IDAT/IEND are accepted (review round 1, finding 5, and
    # round 2, finding 3): the old "ancillary chunks pass" rule is gone,
    # and so is the PLTE exception.
    assert evidence_checks.validate_png(_png_chunks()) == []


# Review round 2, finding 3: Chrome screenshots are RGB/RGBA and never use
# palettes, so PLTE is refused outright (and colour type 3 with the other
# non-RGB/RGBA types), and IDAT chunks must be consecutive.


def test_one_byte_plte_after_ihdr_is_rejected():
    assert evidence_checks.validate_png(
        _png_chunks(extra_before_idat=[(b"PLTE", b"x")])
    )


def test_million_byte_plte_is_rejected():
    assert evidence_checks.validate_png(
        _png_chunks(extra_before_idat=[(b"PLTE", bytes(1_000_000))])
    )


def test_duplicate_plte_is_rejected():
    assert evidence_checks.validate_png(
        _png_chunks(
            extra_before_idat=[(b"PLTE", bytes([1, 2, 3]))] * 2,
        )
    )


def test_plte_after_idat_is_rejected():
    assert evidence_checks.validate_png(
        _png_chunks(extra_after_idat=[(b"PLTE", bytes([1, 2, 3]))])
    )


def test_idat_split_by_plte_is_rejected():
    raw = b"".join(b"\x00" + bytes([1, 2, 3, 4]) * 4 for _ in range(4))
    blob = zlib.compress(raw)
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", blob[: len(blob) // 2])
        + _chunk(b"PLTE", bytes([1, 2, 3]))
        + _chunk(b"IDAT", blob[len(blob) // 2 :])
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data)


def test_consecutive_idat_chunks_pass():
    # Splitting the one zlib stream over several IDAT chunks is legal PNG;
    # only interleaved chunks break consecutiveness.
    raw = b"".join(b"\x00" + bytes([1, 2, 3, 4]) * 4 for _ in range(4))
    blob = zlib.compress(raw)
    ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
    data = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", blob[: len(blob) // 2])
        + _chunk(b"IDAT", blob[len(blob) // 2 :])
        + _chunk(b"IEND", b"")
    )
    assert evidence_checks.validate_png(data) == []


def test_palette_colour_type_is_rejected():
    assert evidence_checks.validate_png(_png_chunks(color_type=3))


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


# Review round 1, finding 5: close the PNG validator gaps. -------------------
#
# Passing today and refused after the fix: a truncated zlib stream, garbage
# after a valid stream, a scanline filter byte of 5, a non-letter chunk
# type, and animation chunks. Chunk types are allow-listed to what the
# pipeline's screenshots contain (a real Chromium capture is pinned below:
# IHDR, IDAT, IEND only), and the IDAT stream must decode to exactly the
# IHDR size with a clean stream end and valid filter bytes.


def _png_idat(width, height, color_type, payload, extra=()):
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + b"".join(_chunk(ctype, body) for ctype, body in extra)
        + _chunk(b"IDAT", payload)
        + _chunk(b"IEND", b"")
    )


def _raw_rgba(width=4, height=4, filt=0):
    return b"".join(
        bytes([filt]) + bytes([9, 9, 9, 255]) * width for _ in range(height)
    )


def test_truncated_zlib_stream_is_rejected():
    raw = _raw_rgba()
    payload = zlib.compress(raw)[:-4]  # adler32 gone, bytes intact
    assert evidence_checks.validate_png(_png_idat(4, 4, 6, payload)) != []


def test_trailing_zlib_data_is_rejected():
    raw = _raw_rgba()
    payload = zlib.compress(raw) + zlib.compress(b"garbage")
    assert evidence_checks.validate_png(_png_idat(4, 4, 6, payload)) != []


# Decision 6 (skip the base captures when nothing capture-affecting changed).
#
# Inverted rule: the skip is allowed ONLY when every path that differs
# between the base and head commits is on the fixed safe list in
# evidence_checks (trusted, ci-guard-protected code shared by the PR-side
# check and main's publisher). Anything else — a new helper under
# scripts/, a top-level sys.path file, a mode change, a symlink — forces a
# full capture. Skip decisions compare the differing set, never file
# contents or PR claims.


def test_safe_skip_list_is_pinned():
    # Narrowing this list forces more full captures (safe); widening it
    # silently skips captures over changed inputs (unsafe). Either way the
    # values are pinned here: changing them is a visible change to this
    # protected test file.
    assert evidence_checks.UI_SKIP_SAFE_DIRS == (
        "docs/",
        "install/",
        "tests/",
        "src/housesitter/",
        ".github/",
    )
    assert evidence_checks.UI_SKIP_REASON == "ui_inputs_identical"
    assert (
        evidence_checks.UI_SKIP_NOTE
        == "Before not captured: UI inputs identical."
    )


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_commit(repo, message):
    _git(
        repo,
        "-c", "user.email=test@example.com",
        "-c", "user.name=evidence-test",
        "commit", "-qm", message,
    )
    done = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def _git_repo_with_paths(tmp_path, name="repo"):
    """A git repo holding one file under each safe and unsafe area."""
    repo = tmp_path / name
    seed = {
        "docs/prose.md": "prose\n",
        "README.md": "readme\n",
        "LICENSE": "license\n",
        "install/setup.sh": "setup\n",
        "tests/test_pins.py": "pins\n",
        "tests/fixtures/dashboard/typical.json": "{}\n",
        "src/housesitter/pkg.py": "pkg\n",
        ".github/workflows/ci.yml": "ci\n",
        ".github/other.yml": "other\n",
        "scripts/dev_dashboard.py": "serve\n",
        "web/index.html": "page\n",
        "pyproject.toml": "build\n",
    }
    for rel, text in seed.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    return repo, _git_commit(repo, "seed")


def _change_and_commit(repo, rel, text="changed\n"):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(repo, "add", "-A")
    return _git_commit(repo, f"change {rel}")


def test_no_changes_allow_the_skip(tmp_path):
    repo, sha = _git_repo_with_paths(tmp_path)
    assert evidence_checks.ui_forcing_changes_git(repo, sha, sha) == []


def test_docs_only_change_skips(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "docs/prose.md")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == []
    repo2, base2 = _git_repo_with_paths(tmp_path, name="repo2")
    head2 = _change_and_commit(repo2, "README.md")
    assert evidence_checks.ui_forcing_changes_git(repo2, base2, head2) == []


def test_unlisted_scripts_helper_forces_a_full_capture(tmp_path):
    # The reviewer's case: scripts/mimetypes.py shadows the stdlib module
    # when dev_dashboard.py runs, changing pixels with no listed input
    # differing. Under the inverted rule any scripts/** addition forces a
    # full capture.
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "scripts/mimetypes.py")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "scripts/mimetypes.py"
    ]


def test_src_sitecustomize_forces_a_full_capture(tmp_path):
    # An editable install puts src/ on sys.path, where Python auto-imports
    # a top-level src/sitecustomize.py at startup. Only paths inside
    # src/housesitter/ are safe.
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "src/sitecustomize.py")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "src/sitecustomize.py"
    ]


def test_dashboard_fixture_change_forces_a_full_capture(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "tests/fixtures/dashboard/typical.json")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "tests/fixtures/dashboard/typical.json"
    ]


def test_mode_only_change_forces_a_full_capture(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    target = repo / "docs" / "prose.md"
    target.chmod(0o755)
    _git(repo, "add", "-A")
    head = _git_commit(repo, "mode only")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "docs/prose.md"
    ]


def test_symlink_forces_a_full_capture(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    (repo / "scripts" / "helper.py").symlink_to("dev_dashboard.py")
    _git(repo, "add", "-A")
    head = _git_commit(repo, "add symlink")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "scripts/helper.py"
    ]


def test_gitlink_forces_a_full_capture(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD:README.md"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{blob},vendor/lib")
    head = _git_commit(repo, "add gitlink")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "vendor/lib"
    ]


def test_new_top_level_file_forces_a_full_capture(tmp_path):
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "sitecustomize.py")
    assert evidence_checks.ui_forcing_changes_git(repo, base, head) == [
        "sitecustomize.py"
    ]


def test_inputs_identical_command_reports_the_decision(tmp_path, capsys):
    repo, sha = _git_repo_with_paths(tmp_path)
    assert (
        evidence_checks.main(
            [
                "inputs-identical",
                "--git-dir", str(repo),
                "--base-sha", sha,
                "--head-sha", sha,
            ]
        )
        == 0
    )
    assert "no capture-affecting change" in capsys.readouterr().out


def test_inputs_identical_command_reports_differences(tmp_path, capsys):
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "scripts/mimetypes.py")
    assert (
        evidence_checks.main(
            [
                "inputs-identical",
                "--git-dir", str(repo),
                "--base-sha", base,
                "--head-sha", head,
            ]
        )
        == 1
    )
    assert "scripts/mimetypes.py" in capsys.readouterr().out


def test_inputs_identical_command_rejects_bad_shas(tmp_path, capsys):
    repo, _ = _git_repo_with_paths(tmp_path)
    assert (
        evidence_checks.main(
            [
                "inputs-identical",
                "--git-dir", str(repo),
                "--base-sha", "nope",
                "--head-sha", "a" * 40,
            ]
        )
        == 2
    )


def _tree_doc(sha, entries):
    """A git-trees API response shape: dir paths carry no trailing slash."""
    kinds = {"web": "tree", "tests/fixtures/dashboard": "tree"}
    return {
        "sha": sha,
        "url": "https://api.github.com/repos/o/n/git/trees/" + sha,
        "tree": [
            {
                "path": path,
                "mode": "040000" if kinds.get(path) == "tree" else "100644",
                "type": kinds.get(path, "blob"),
                "sha": "e" * 40 if kinds.get(path) == "tree" else "b" * 40,
            }
            for path in entries
        ],
        "truncated": False,
    }


def test_parse_tree_entries_keeps_blobs_and_skips_trees():
    sha = "a" * 40
    doc = _tree_doc(sha, ["web", "scripts/ui_evidence.py", "README.md"])
    mapping = evidence_checks.parse_tree_entries(doc, sha)
    assert "web" not in mapping  # a tree rollup, not a capture input
    assert mapping["scripts/ui_evidence.py"] == ("100644", "blob", "b" * 40)
    assert mapping["README.md"] == ("100644", "blob", "b" * 40)


def test_parse_tree_entries_refuses_a_truncated_listing():
    sha = "a" * 40
    doc = _tree_doc(sha, ["web"])
    doc["truncated"] = True
    with pytest.raises(ValueError):
        evidence_checks.parse_tree_entries(doc, sha)


def test_parse_tree_entries_refuses_a_mismatched_sha():
    doc = _tree_doc("a" * 40, ["web"])
    with pytest.raises(ValueError):
        evidence_checks.parse_tree_entries(doc, "b" * 40)


def test_parse_tree_entries_refuses_bad_shapes():
    with pytest.raises(ValueError):
        evidence_checks.parse_tree_entries({"tree": []}, "a" * 40)
    with pytest.raises(ValueError):
        evidence_checks.parse_tree_entries(
            {"sha": "a" * 40, "tree": [{"path": "web"}]}, "a" * 40
        )
    with pytest.raises(ValueError):
        evidence_checks.parse_tree_entries(
            {
                "sha": "a" * 40,
                "tree": [
                    {
                        "path": "web/index.html",
                        "mode": "100644",
                        "type": "blob",
                        "sha": "xyz",
                    }
                ],
            },
            "a" * 40,
        )


def _entries(*paths, sha="b" * 40, mode="100644", typ="blob"):
    return {path: (mode, typ, sha) for path in paths}


def test_check_ui_skip_accepts_identical_trees():
    base = _entries("web/index.html", "scripts/ui_evidence.py")
    assert (
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, dict(base), dict(base))
        is None
    )


def test_check_ui_skip_accepts_safe_only_differences():
    base = _entries("docs/prose.md", "web/index.html")
    head = _entries("docs/prose.md", "web/index.html", sha="d" * 40)
    head["web/index.html"] = ("100644", "blob", "b" * 40)
    assert (
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, dict(base), dict(head))
        is None
    )


def test_check_ui_skip_refuses_an_unsafe_difference():
    base = _entries("scripts/mimetypes.py")
    head = _entries("scripts/mimetypes.py", sha="d" * 40)
    with pytest.raises(ValueError, match="scripts/mimetypes.py"):
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, base, head)


def test_check_ui_skip_refuses_a_mode_only_change():
    base = {"docs/prose.md": ("100644", "blob", "b" * 40)}
    head = {"docs/prose.md": ("100755", "blob", "b" * 40)}
    with pytest.raises(ValueError, match="docs/prose.md"):
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, base, head)


def test_check_ui_skip_refuses_a_symlink():
    base = {}
    head = {"scripts/helper.py": ("120000", "blob", "b" * 40)}
    with pytest.raises(ValueError, match="scripts/helper.py"):
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, base, head)


def test_check_ui_skip_refuses_a_gitlink():
    base = {}
    head = {"vendor/lib": ("160000", "commit", "b" * 40)}
    with pytest.raises(ValueError, match="vendor/lib"):
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, base, head)


def test_check_ui_skip_refuses_a_new_top_level_file():
    with pytest.raises(ValueError, match="sitecustomize.py"):
        evidence_checks.check_ui_skip(
            "a" * 40,
            "c" * 40,
            {},
            {"sitecustomize.py": ("100644", "blob", "b" * 40)},
        )


def test_check_ui_skip_refuses_unverifiable_maps():
    with pytest.raises(ValueError):
        evidence_checks.check_ui_skip("a" * 40, "c" * 40, None, {})
    with pytest.raises(ValueError):
        evidence_checks.check_ui_skip("nope", "c" * 40, {}, {})


def test_manifest_claims_skip_only_with_the_reason(tmp_path):
    ui_dir = tmp_path / "ui_out"
    ui_dir.mkdir()
    assert evidence_checks.manifest_claims_skip(ui_dir) is False
    (ui_dir / "ui_manifest.json").write_text(
        json.dumps({"version": 1, "captures": []})
    )
    assert evidence_checks.manifest_claims_skip(ui_dir) is False
    (ui_dir / "ui_manifest.json").write_text(
        json.dumps(
            {
                "version": 1,
                "base_skipped": "ui_inputs_identical",
                "captures": [],
            }
        )
    )
    assert evidence_checks.manifest_claims_skip(ui_dir) is True


def test_manifest_claims_skip_refuses_an_oversized_manifest_without_reading_it(
    tmp_path, monkeypatch
):
    # F4: a hostile 2 MB manifest must be refused before it is read in full.
    ui_dir = tmp_path / "ui_out"
    ui_dir.mkdir()
    manifest = ui_dir / "ui_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "base_skipped": "ui_inputs_identical",
                "captures": [],
                "padding": "x" * 2_000_000,
            }
        )
    )
    reads = []
    real_read_text = Path.read_text

    def record(self, *args, **kwargs):
        if self == manifest:
            reads.append(self.stat().st_size)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record)
    assert evidence_checks.manifest_claims_skip(ui_dir) is False
    assert reads == []


def test_manifest_claims_skip_ignores_a_symlink_and_bad_json(tmp_path):
    ui_dir = tmp_path / "ui_out"
    ui_dir.mkdir()
    real = ui_dir / "real.json"
    real.write_text(json.dumps({"version": 1, "base_skipped": "x", "captures": []}))
    link = ui_dir / "ui_manifest.json"
    link.symlink_to(real)
    assert evidence_checks.manifest_claims_skip(ui_dir) is False
    link.unlink()
    link.write_text("{not json")
    assert evidence_checks.manifest_claims_skip(ui_dir) is False
    link.write_text(json.dumps({"version": 1, "captures": []}))
    assert evidence_checks.manifest_claims_skip(ui_dir) is False


def _load_ui_evidence():
    spec = importlib.util.spec_from_file_location(
        "ui_evidence_cap_pin", ROOT / "scripts" / "ui_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skip_preflight_cap_matches_the_manifest_cap():
    assert (
        evidence_checks.UI_MANIFEST_MAX_BYTES
        == _load_ui_evidence().MANIFEST_MAX_BYTES
    )


def test_safe_paths_cover_the_documented_areas():
    safe = [
        "docs/anything.md",
        "docs/nested/page.md",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "LICENSE.md",
        "install/setup.sh",
        "install/nested/script.sh",
        "tests/test_pins.py",
        "tests/conformance/demo.py",
        "src/housesitter/pkg.py",
        "src/housesitter/nested/mod.py",
        ".github/workflows/other.yml",
        ".github/ISSUE_TEMPLATE/bug.md",
    ]
    for path in safe:
        assert evidence_checks.skip_safe_path(path), path


def test_unsafe_paths_force_a_full_capture():
    unsafe = [
        # Served or executed by the capture path.
        "web/index.html",
        "scripts/dev_dashboard.py",
        "scripts/ui_evidence.py",
        "scripts/mimetypes.py",
        "scripts/evidence_checks.py",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        # The captures render these fixtures.
        "tests/fixtures/dashboard/typical.json",
        "tests/fixtures/dashboard",
        # src/ sits on sys.path for the editable install; only the
        # package directory itself is safe.
        "src/sitecustomize.py",
        "src/conftest.py",
        # New top-level files and directories, and anything unsure.
        "sitecustomize.py",
        "newdir/file.py",
        "src",
        "tests",
    ]
    for path in unsafe:
        assert not evidence_checks.skip_safe_path(path), path


def _skip_gate_artifact(tmp_path, repo, base, head):
    """An artifact whose manifest claims the base captures were skipped."""
    artifact = _gate_artifact(tmp_path)
    doc = json.loads((artifact / "results.json").read_text())
    doc["metadata"]["head_sha"] = head
    (artifact / "results.json").write_text(json.dumps(doc))
    manifest = json.loads((artifact / "ui_out" / "ui_manifest.json").read_text())
    manifest["captures"] = [
        {
            "fixture": "typical",
            "view": "index-phone",
            "status": "unchanged",
            "base": None,
            "head": None,
            "diff": None,
            "head_problems": [],
        }
    ]
    manifest["base_skipped"] = "ui_inputs_identical"
    manifest["base_sha"] = base
    manifest["head_sha"] = head
    (artifact / "ui_out" / "ui_manifest.json").write_text(json.dumps(manifest))
    for png in (artifact / "ui_out").glob("*.png"):
        png.unlink()
    return artifact


def _check_skip(artifact, repo, base, head):
    return evidence_checks.main(
        [
            "check",
            "--artifact-dir", str(artifact),
            "--repo", "example-owner/example-repo",
            "--pr", "7",
            "--head-sha", head,
            "--base-sha", base,
            "--git-dir", str(repo),
        ]
    )


def test_check_accepts_a_verified_skip(tmp_path, capsys):
    repo, sha = _git_repo_with_paths(tmp_path)
    artifact = _skip_gate_artifact(tmp_path, repo, sha, sha)
    assert _check_skip(artifact, repo, sha, sha) == 0


def test_check_refuses_a_skip_with_differing_inputs(tmp_path, capsys):
    repo, base = _git_repo_with_paths(tmp_path)
    head = _change_and_commit(repo, "scripts/added.py")
    artifact = _skip_gate_artifact(tmp_path, repo, base, head)
    assert _check_skip(artifact, repo, base, head) == 1
    assert "bad_skip" in capsys.readouterr().out


def test_check_refuses_a_skip_claimed_for_another_base(tmp_path, capsys):
    # F2: the manifest must record the base it was compared against. A
    # manifest that names a different base than the run's own is refused,
    # even when the trees for that other base would allow the skip.
    repo, sha = _git_repo_with_paths(tmp_path)
    other = "d" * 40
    artifact = _skip_gate_artifact(tmp_path, repo, other, sha)
    assert _check_skip(artifact, repo, sha, sha) == 1
    assert "bad_skip" in capsys.readouterr().out


def test_check_refuses_a_skip_it_cannot_verify(tmp_path, capsys):
    repo, sha = _git_repo_with_paths(tmp_path)
    artifact = _skip_gate_artifact(tmp_path, repo, sha, sha)
    assert (
        evidence_checks.main(
            [
                "check",
                "--artifact-dir", str(artifact),
                "--repo", "example-owner/example-repo",
                "--pr", "7",
                "--head-sha", sha,
            ]
        )
        == 1
    )
    assert "bad_skip" in capsys.readouterr().out


def test_invalid_filter_byte_is_rejected():
    payload = zlib.compress(_raw_rgba(filt=5))
    assert evidence_checks.validate_png(_png_idat(4, 4, 6, payload)) != []


def test_non_letter_chunk_type_is_rejected():
    raw = _raw_rgba()
    data = _png_idat(4, 4, 6, zlib.compress(raw), extra=[(b"1234", b"")])
    assert evidence_checks.validate_png(data) != []


def test_animation_chunks_are_rejected():
    raw = _raw_rgba()
    extra = [
        (b"acTL", struct.pack(">II", 1, 0)),
        (b"fcTL", struct.pack(">IIIII", 0, 4, 4, 0, 0) + b"\x00\x00"),
        (b"fdAT", struct.pack(">I", 0) + zlib.compress(raw)),
    ]
    data = _png_idat(4, 4, 6, zlib.compress(raw), extra=extra)
    assert evidence_checks.validate_png(data) != []


def test_unlisted_ancillary_chunk_is_rejected():
    raw = _raw_rgba()
    data = _png_idat(4, 4, 6, zlib.compress(raw), extra=[(b"tEXt", b"comment")])
    assert evidence_checks.validate_png(data) != []


def test_real_chromium_screenshot_passes():
    # Captured locally with Playwright's bundled Chromium (no house data:
    # a blank probe page), checked in so the allow-list cannot drift past
    # what the pipeline's screenshots contain. CI's Google Chrome uses the
    # same screenshot encoder; the green run confirms it still validates.
    shot = (ROOT / "tests" / "fixtures" / "png" / "chrome-screenshot.png").read_bytes()
    assert evidence_checks.validate_png(shot) == []
