"""Tests for scripts/ui_evidence.py. No browser needed except the e2e test."""

from __future__ import annotations

import importlib.util
import json
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("ui_evidence", ROOT / "scripts" / "ui_evidence.py")
ui_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ui_evidence)

PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
needs_browser = pytest.mark.skipif(not PLAYWRIGHT, reason="Playwright is not installed")

REPO = "example-owner/example-repo"
HEAD_SHA = "a" * 40


def rgb_png(width: int, height: int, color: tuple = (10, 20, 30)) -> bytes:
    r, g, b = color
    return ui_evidence.encode_png(width, height, 2, bytes([r, g, b]) * width * height)


def rgba_png(width: int, height: int) -> bytes:
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels += bytes([x % 256, y % 256, (x + y) % 256, 200])
    return ui_evidence.encode_png(width, height, 6, bytes(pixels))


# PNG codec ---------------------------------------------------------------


def test_png_roundtrip_rgb():
    shot = rgb_png(7, 5)
    width, height, pixels = ui_evidence.decode_png(shot)
    assert (width, height) == (7, 5)
    assert pixels == bytes([10, 20, 30, 255]) * 35


def test_png_roundtrip_rgba():
    shot = rgba_png(9, 4)
    width, height, pixels = ui_evidence.decode_png(shot)
    assert (width, height) == (9, 4)
    assert pixels[3] == 200
    assert pixels[0:3] == b"\x00\x00\x00"


def test_png_roundtrip_filters():
    # A gradient exercises Sub/Up/Average/Paeth filters on decode.
    pixels = bytes([(x * 13 + y * 29) % 256 for y in range(6) for x in range(8) for _ in "rgb"])
    shot = ui_evidence.encode_png(8, 6, 2, pixels)
    width, height, decoded = ui_evidence.decode_png(shot)
    assert (width, height) == (8, 6)
    assert decoded == b"".join(
        pixels[i:i + 3] + b"\xff" for i in range(0, len(pixels), 3)
    )


def test_decode_rejects_non_png():
    with pytest.raises(ValueError):
        ui_evidence.decode_png(b"definitely not a png")


def test_decode_rejects_interlaced():
    shot = bytearray(rgb_png(2, 2))
    shot[28] = 1  # interlace byte of IHDR
    with pytest.raises(ValueError, match="interlace"):
        ui_evidence.decode_png(bytes(shot))


def test_decode_rejects_truncated():
    with pytest.raises(ValueError):
        ui_evidence.decode_png(rgb_png(4, 4)[:40])


def test_encode_rejects_bad_length():
    with pytest.raises(ValueError):
        ui_evidence.encode_png(2, 2, 2, b"\x00" * 5)


# Diff and classification --------------------------------------------------


def test_identical_pixels_are_unchanged():
    assert ui_evidence.diff_rgba(ui_evidence.decode_png(rgb_png(3, 3)),
                                 ui_evidence.decode_png(rgb_png(3, 3))) == (False, None)
    assert ui_evidence.classify_pair(rgb_png(3, 3), rgb_png(3, 3)) == "unchanged"


def test_same_pixels_different_bytes_are_unchanged():
    # Opaque RGBA pixels vs the same pixels re-encoded as RGB: different
    # bytes, identical pixels. Classification must compare pixels, not bytes.
    rgba = bytes([10, 20, 30, 255]) * 16
    original = ui_evidence.encode_png(4, 4, 6, rgba)
    rgb = b"".join(rgba[i:i + 3] for i in range(0, len(rgba), 4))
    re_encoded = ui_evidence.encode_png(4, 4, 2, rgb)
    assert original != re_encoded
    assert ui_evidence.classify_pair(original, re_encoded) == "unchanged"


def test_one_changed_pixel_is_changed_with_red_diff():
    before = rgb_png(4, 4)
    after = rgb_png(4, 4, (200, 1, 1))
    changed, diff = ui_evidence.diff_rgba(
        ui_evidence.decode_png(before), ui_evidence.decode_png(after)
    )
    assert changed
    assert ui_evidence.classify_pair(before, after) == "changed"
    width, height, rgb = diff
    assert (width, height) == (4, 4)
    assert rgb[:3] == b"\xff\x00\x00"
    reshot = ui_evidence.encode_png(width, height, 2, rgb)
    assert ui_evidence.decode_png(reshot)[:2] == (4, 4)


def test_size_mismatch_is_changed():
    changed, diff = ui_evidence.diff_rgba(
        ui_evidence.decode_png(rgb_png(2, 2)), ui_evidence.decode_png(rgb_png(3, 2))
    )
    assert changed
    assert diff[0:2] == (3, 2)


def test_added_removed_and_empty():
    assert ui_evidence.classify_pair(None, rgb_png(2, 2)) == "added"
    assert ui_evidence.classify_pair(rgb_png(2, 2), None) == "removed"
    with pytest.raises(ValueError):
        ui_evidence.classify_pair(None, None)


# Checks --------------------------------------------------------------------


class FakePage:
    def __init__(self, problems=(), overflow=False, clipped=()):
        self.problems = list(problems)
        self._overflow = overflow
        self._clipped = list(clipped)

    def evaluate(self, script: str):
        if script == ui_evidence.OVERFLOW_JS:
            return self._overflow
        return self._clipped


def test_clean_page_passes():
    assert ui_evidence.check_page(FakePage(), "index-phone", rgb_png(2, 2)) == []


def test_console_error_warning_pageerror_and_dialog_fail():
    problems = ui_evidence.check_page(
        FakePage(problems=[
            ("error", "boom"),
            ("warning", "slow"),
            ("pageerror", "TypeError: x"),
            ("dialog", "are you sure?"),
        ]),
        "index-phone",
        rgb_png(2, 2),
    )
    assert problems == [
        "console.error: boom",
        "console.warning: slow",
        "pageerror: TypeError: x",
        "dialog: are you sure?",
    ]


def test_csp_violation_is_labelled():
    (problem,) = ui_evidence.check_page(
        FakePage(problems=[
            ("error", "Refused to run script because Content Security Policy blocks it"),
        ]),
        "index-phone",
        rgb_png(2, 2),
    )
    assert problem.startswith("csp-or-trusted-types: ")


def test_overflow_and_clipping_fail():
    problems = ui_evidence.check_page(
        FakePage(overflow=True, clipped=["DIV.card", "SPAN.x"]),
        "index-phone",
        rgb_png(2, 2),
    )
    assert "horizontal page overflow" in problems
    assert "internal clipping: DIV.card" in problems
    assert "internal clipping: SPAN.x" in problems


def test_eink_must_be_exactly_800x480():
    ok = ui_evidence.encode_png(800, 480, 2, b"\x00" * 800 * 480 * 3)
    assert ui_evidence.check_page(FakePage(), "eink", ok) == []
    bad = ui_evidence.check_page(FakePage(), "eink", rgb_png(10, 10))
    assert bad == ["e-ink shot is 10x10, not 800x480"]
    broken = ui_evidence.check_page(FakePage(), "eink", b"junk")
    assert broken and broken[0].startswith("undecodable PNG: ")


# Comment section ------------------------------------------------------------


def make_manifest(status="added", problems=(), fixture="typical", view="index-phone"):
    name = f"{fixture}__{view}__head.png"
    return {
        "version": 1,
        "captures": [
            {
                "fixture": fixture,
                "view": view,
                "status": status,
                "base": None,
                "head": name,
                "diff": None,
                "head_problems": list(problems),
            }
        ],
    }


def test_section_table_and_images():
    full, table = ui_evidence.build_ui_section(make_manifest(), REPO, 7, HEAD_SHA, 60_000, False)
    assert "## Web UI" in table
    assert "| `typical / index-phone` | added | pass |" in table
    assert "<details><summary>typical</summary>" in full
    assert "__HOUSESITTER_EVIDENCE_SHA__" in full
    assert "no page" in full
    pinned, _ = ui_evidence.build_ui_section(
        make_manifest(), REPO, 7, HEAD_SHA, 60_000, False
    )
    pinned = ui_evidence.substitute_sha(pinned, HEAD_SHA)
    assert "__HOUSESITTER_EVIDENCE_SHA__" not in pinned
    assert HEAD_SHA in pinned


def test_section_budget_drops_details_first():
    manifest = {
        "version": 1,
        "captures": [
            dict(make_manifest(fixture=f"f{i:02d}")["captures"][0]) for i in range(10)
        ],
    }
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 3000, True)
    assert len(full.encode()) <= 3000
    assert "**Omitted to fit a GitHub comment:**" in full
    assert "**Omitted to fit a GitHub comment:**" in full
    assert table.count("<details>") == 0
    assert full.count("<details>") == full.count("</details>")


def test_section_tolerates_odd_fixture_names_without_images():
    # Real fixture names come from filenames, but a hostile one must not
    # break the comment or smuggle markup: unchanged captures render only in
    # the Markdown table (a code span), never as an <img> (img_tag refuses
    # names outside the allow-list, covered above).
    manifest = make_manifest(fixture="a<b", status="unchanged")
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert "| `a<b / index-phone` | unchanged |" in table
    assert full.count("<details>") == full.count("</details>")


def test_img_tag_only_for_allowed_names():
    tag = ui_evidence.img_tag("https://x/typical__eink__head.png", 400, 'a"b<c')
    assert 'alt="a&quot;b&lt;c"' in tag
    with pytest.raises(ValueError):
        ui_evidence.img_tag("https://x/evil.png", 400, "nope")
    with pytest.raises(ValueError):
        ui_evidence.img_tag("https://x/ui_section.md", 400, "nope")


def test_substitute_sha_validates():
    with pytest.raises(ValueError):
        ui_evidence.substitute_sha("x", "not-a-sha")
    with pytest.raises(ValueError):
        ui_evidence.base_url("not a repo!", 1, HEAD_SHA)
    with pytest.raises(ValueError):
        ui_evidence.base_url(REPO, 0, HEAD_SHA)


def test_pr_evidence_builds_ui_section_from_manifest(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "pr_evidence", ROOT / "scripts" / "pr_evidence.py"
    )
    pr_evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr_evidence)

    ui_dir = tmp_path / "ui"
    ui_dir.mkdir()
    (ui_dir / "ui_manifest.json").write_text(json.dumps(make_manifest()))
    kwargs = {"repo": REPO, "pr": 7, "head_sha": HEAD_SHA}
    # Pre-rendered markdown is ignored: only the manifest matters.
    (ui_dir / "ui_section.md").write_text("PWNED\n")
    body = pr_evidence.append_ui_section("text", ui_dir, **kwargs)
    assert "## Web UI" in body
    assert "PWNED" not in body
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 1100)
    body = pr_evidence.append_ui_section("text", ui_dir, **kwargs)
    assert "## Web UI" in body
    assert "details omitted" in body
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 100)
    body = pr_evidence.append_ui_section("text", ui_dir, **kwargs)
    assert "evidence omitted" in body
    assert pr_evidence.append_ui_section("text", None, **kwargs) == "text"
    assert pr_evidence.append_ui_section("text", tmp_path / "missing", **kwargs) == "text"
    # Without repo context the pinned image URLs cannot be built.
    assert pr_evidence.append_ui_section("text", ui_dir) == "text"


# Artifact validator ----------------------------------------------------------


def write_valid_artifact(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    name = "typical__index-phone__head.png"
    (directory / name).write_bytes(rgb_png(4, 4))
    (directory / ui_evidence.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "version": 1,
                "captures": [
                    {
                        "fixture": "typical",
                        "view": "index-phone",
                        "status": "added",
                        "base": None,
                        "head": name,
                        "diff": None,
                        "head_problems": [],
                    }
                ],
            }
        )
    )
    return directory


def test_validator_caps_cover_a_full_capture_run():
    # A full run (every current fixture x every view x three sides) must fit
    # the caps with room to spare, or legitimate evidence fails validation.
    fixtures = ["typical", "empty", "edge", "large", "error", "failed", "hostile"]
    views = ["index-phone", "index-desktop", "index-desktop-dark", "eink"]
    sides = ["base", "head", "diff"]
    assert len(fixtures) * len(views) * len(sides) + 1 < ui_evidence.MAX_FILE_COUNT


def test_validator_accepts_a_good_directory(tmp_path):
    assert ui_evidence.validate_artifact(write_valid_artifact(tmp_path / "ok")) == []


def test_validator_rejects_symlink_subdir_and_bad_name(tmp_path):
    directory = write_valid_artifact(tmp_path / "bad")
    (directory / "typical__eink__head.png").symlink_to(directory / "typical__index-phone__head.png")
    (directory / "notes").mkdir()
    (directory / "notes.txt").write_text("hi")
    errors = ui_evidence.validate_artifact(directory)
    assert any("symlink" in e for e in errors)
    assert any("unexpected directory" in e for e in errors)
    assert any("unexpected file name" in e for e in errors)


def test_validator_rejects_non_png(tmp_path):
    directory = write_valid_artifact(tmp_path / "flat")
    (directory / "typical__eink__head.png").write_bytes(b"not a png at all")
    errors = ui_evidence.validate_artifact(directory)
    assert any("not a PNG" in e for e in errors)


def test_validator_rejects_oversize_file(tmp_path, monkeypatch):
    directory = write_valid_artifact(tmp_path / "big")
    monkeypatch.setattr(ui_evidence, "MAX_FILE_BYTES", 10)
    errors = ui_evidence.validate_artifact(directory)
    assert any("over the cap" in e for e in errors)


def test_validator_rejects_traversal_and_missing_manifest_names(tmp_path):
    directory = write_valid_artifact(tmp_path / "trav")
    manifest = json.loads((directory / ui_evidence.MANIFEST_NAME).read_text())
    manifest["captures"][0]["head"] = "../typical__index-phone__head.png"
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    assert any("escapes" in e for e in ui_evidence.validate_artifact(directory))
    manifest["captures"][0]["head"] = "typical__eink__head.png"
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    assert any("missing file" in e for e in ui_evidence.validate_artifact(directory))
    (directory / ui_evidence.MANIFEST_NAME).unlink()
    assert any("missing manifest" in e for e in ui_evidence.validate_artifact(directory))


def test_validator_rejects_section_files(tmp_path):
    # Only PNGs and the manifest may be pushed; the .md files stay in the comment.
    directory = write_valid_artifact(tmp_path / "md")
    (directory / "ui_section.md").write_text("## Web UI\n")
    assert any("unexpected file name" in e for e in ui_evidence.validate_artifact(directory))


@pytest.mark.parametrize("captures", [{}, "x", None])
def test_validator_rejects_missing_or_non_list_captures(tmp_path, captures):
    # Issue #22: the publisher's loader requires a captures list, so the
    # validator must too; otherwise a bad artifact exits 2, not 1.
    directory = write_valid_artifact(tmp_path / "nocaptures")
    manifest = json.loads((directory / ui_evidence.MANIFEST_NAME).read_text())
    if captures is None:
        del manifest["captures"]
    else:
        manifest["captures"] = captures
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    assert any("captures list" in e for e in ui_evidence.validate_artifact(directory))


@pytest.mark.parametrize("entry", [42, "x", None])
def test_validator_names_invalid_capture_entries_plainly(tmp_path, entry):
    # Valid JSON with a junk entry: fail closed, with a message that says so
    # (not "not valid JSON", and no Python internals).
    directory = write_valid_artifact(tmp_path / "badentry")
    manifest = json.loads((directory / ui_evidence.MANIFEST_NAME).read_text())
    manifest["captures"].append(entry)
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    errors = ui_evidence.validate_artifact(directory)
    assert "manifest has an invalid capture entry" in errors


def test_validator_caps_echoed_manifest_names(tmp_path):
    directory = write_valid_artifact(tmp_path / "longname")
    manifest = json.loads((directory / ui_evidence.MANIFEST_NAME).read_text())
    manifest["captures"][0]["head"] = "A" * 10_000
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    errors = ui_evidence.validate_artifact(directory)
    assert errors and all(len(e) < 300 for e in errors)


# Manifest building with undecodable PNGs ----------------------------------------


def _unsupported_variant(kind: str) -> bytes:
    """A structurally valid PNG the stdlib decoder must refuse."""
    shot = bytearray(rgb_png(4, 4))
    if kind == "palette":
        shot[25] = 3  # IHDR colour type: palette
    elif kind == "sixteen-bit":
        shot[24] = 16  # IHDR bit depth
    elif kind == "interlaced":
        shot[28] = 1  # IHDR interlace method
    else:
        raise AssertionError(kind)
    return bytes(shot)


def _manifest_trees(tmp_path: Path, name: str = "trees"):
    base = tmp_path / name / "base"
    head = tmp_path / name / "head"
    for tree in (base, head):
        (tree / "web").mkdir(parents=True)
        (tree / "web" / "index.html").write_text("<html></html>")
        (tree / "web" / "eink.html").write_text("<html></html>")
        (tree / "tests" / "fixtures" / "dashboard").mkdir(parents=True)
        (tree / "tests" / "fixtures" / "dashboard" / "typical.json").write_text("{}")
    return base, head


VIEWS_UNDER_TEST = ("index-phone", "index-desktop", "index-desktop-dark", "eink")


def _write_pair(out: Path, base_shot: bytes | None, head_shot: bytes | None) -> None:
    for view in VIEWS_UNDER_TEST:
        if base_shot is not None:
            (out / f"typical__{view}__base.png").write_bytes(base_shot)
        if head_shot is not None:
            (out / f"typical__{view}__head.png").write_bytes(head_shot)


def _phone_capture(manifest: dict) -> dict:
    return next(c for c in manifest["captures"] if c["view"] == "index-phone")


@pytest.mark.parametrize("kind", ["palette", "sixteen-bit", "interlaced"])
def test_unsupported_png_is_changed_without_a_diff(tmp_path, kind):
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _write_pair(out, rgb_png(4, 4), _unsupported_variant(kind))
    manifest = ui_evidence.build_manifest(base, head, out, {})
    capture = _phone_capture(manifest)
    assert capture["status"] == "changed"
    assert capture["diff"] is None
    assert "diff unavailable" in (capture.get("diff_error") or "")
    # The manifest is still written, so the evidence job can report the pair.
    assert json.loads(json.dumps(manifest))["captures"][0]["status"] == "changed"
    assert not (out / "typical__index-phone__diff.png").exists()


def test_changed_pair_with_decodable_pngs_still_writes_a_diff(tmp_path):
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    _write_pair(out, rgb_png(4, 4), rgb_png(4, 4, (200, 1, 1)))
    manifest = ui_evidence.build_manifest(base, head, out, {})
    capture = _phone_capture(manifest)
    assert capture["status"] == "changed"
    assert capture["diff"] == "typical__index-phone__diff.png"
    assert (out / "typical__index-phone__diff.png").is_file()


def test_changed_pair_is_decoded_once_per_side(tmp_path, monkeypatch):
    # Classification already decodes both sides; the diff image reuses that
    # work instead of decoding both PNGs again.
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    before, after = rgba_png(6, 5), rgb_png(6, 5, (200, 1, 1))
    _write_pair(out, before, after)
    want_diff = ui_evidence.diff_rgba(
        ui_evidence.decode_png(before), ui_evidence.decode_png(after)
    )[1]
    calls = []
    real_decode = ui_evidence.decode_png

    def counting_decode(data):
        calls.append(data)
        return real_decode(data)

    monkeypatch.setattr(ui_evidence, "decode_png", counting_decode)
    manifest = ui_evidence.build_manifest(base, head, out, {})
    assert [c["status"] for c in manifest["captures"]] == ["changed"] * len(VIEWS_UNDER_TEST)
    assert calls.count(before) == len(VIEWS_UNDER_TEST)
    assert calls.count(after) == len(VIEWS_UNDER_TEST)
    assert len(calls) == 2 * len(VIEWS_UNDER_TEST)
    w, h, rgb = want_diff
    for view in VIEWS_UNDER_TEST:
        diff_png = (out / f"typical__{view}__diff.png").read_bytes()
        assert diff_png == ui_evidence.encode_png(w, h, 2, rgb)


def test_unchanged_pair_drops_its_pngs(tmp_path):
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    shot = rgb_png(4, 4)
    _write_pair(out, shot, shot)
    manifest = ui_evidence.build_manifest(base, head, out, {})
    assert manifest["captures"][0]["status"] == "unchanged"
    assert not (out / "typical__index-phone__base.png").exists()
    assert not (out / "typical__index-phone__head.png").exists()


def test_no_web_on_either_side_is_zero_captures(tmp_path):
    # The evidence code must work when web/ does not exist on base and/or
    # head (main has no web/ yet): zero captures, no failure.
    base = tmp_path / "base"
    head = tmp_path / "head"
    for tree in (base, head):
        tree.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    assert ui_evidence.has_web(base) is False
    assert ui_evidence.has_web(head) is False
    manifest = ui_evidence.build_manifest(base, head, out, {})
    assert manifest == {"version": 1, "browser": None, "captures": []}
    assert ui_evidence.validate_artifact(out) != []  # no manifest written
    (out / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    assert ui_evidence.validate_artifact(out) == []


# Capture-name rule -----------------------------------------------------------------


def test_capture_name_rule():
    assert ui_evidence.CAPTURE_RE.fullmatch("typical__index-phone__head.png")
    assert ui_evidence.CAPTURE_RE.fullmatch("a_b-c__eink__diff.png")
    assert ui_evidence.CAPTURE_RE.fullmatch("x" * 48 + "__index-desktop__base.png")
    # Double underscores would make the fixture/view split ambiguous.
    assert not ui_evidence.CAPTURE_RE.fullmatch("a__b__index-phone__head.png")
    assert not ui_evidence.CAPTURE_RE.fullmatch("Typical__index-phone__head.png")
    assert not ui_evidence.CAPTURE_RE.fullmatch("x" * 49 + "__eink__diff.png")


# Height cap ------------------------------------------------------------------------


def test_page_height_check_cap_covers_the_stress_fixture():
    # 120,000 covers the committed fixtures with headroom: the 600-client
    # stress fixture renders ~104,200 CSS px tall on the phone view, so the
    # suggested 16,000 would fail every run on legitimate evidence.
    assert ui_evidence.MAX_PAGE_HEIGHT_PX == 120_000


def test_saved_shots_are_cropped_well_under_the_file_cap():
    # Full-height shots of the 600-client fixture were ~9 MB in Google Chrome,
    # over MAX_FILE_BYTES, so main's publisher refused the whole artifact.
    # Cropping is independent of the page-height check.
    assert ui_evidence.MAX_SHOT_HEIGHT_PX == 8_000
    assert ui_evidence.MAX_SHOT_HEIGHT_PX < ui_evidence.MAX_PAGE_HEIGHT_PX
    # Widest view x crop height x 4 bytes, raw: compressed PNGs of real pages
    # are far smaller, and the e2e test checks actual sizes.
    assert 1280 * ui_evidence.MAX_SHOT_HEIGHT_PX * 4 < 6 * ui_evidence.MAX_FILE_BYTES


def test_tall_shot_is_cropped(monkeypatch):
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    wide = 2
    tall_shot = ui_evidence.encode_png(wide, 12, 2, b"\x01\x02\x03" * wide * 12)
    cropped, was_cropped = ui_evidence.clip_to_max_height(tall_shot)
    assert was_cropped is True
    width, height, _ = ui_evidence.decode_png(cropped)
    assert (width, height) == (wide, 10)
    small = rgb_png(3, 3)
    assert ui_evidence.clip_to_max_height(small) == (small, False)


def _no_decode(_data):
    raise AssertionError("decode_png must not run for a shot under the cap")


def test_short_shot_is_returned_without_decoding(monkeypatch):
    # The PNG header (IHDR) already holds the height, so a shot at or under
    # the cap is never decoded: decoding a full-page shot in pure Python
    # just to read its height took ~2 s per shot in CI.
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    monkeypatch.setattr(ui_evidence, "decode_png", _no_decode)
    for shot in (rgb_png(3, 3), rgba_png(5, 10)):
        assert ui_evidence.clip_to_max_height(shot) == (shot, False)


def test_short_shot_with_bad_image_data_is_returned_unchanged(monkeypatch):
    # Only the header is read for a short shot. Before, bad image data raised
    # ValueError and the capture loop kept the bytes; the result is the same.
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    shot = bytearray(rgb_png(4, 4))
    idat = shot.index(b"IDAT")
    shot[idat + 4:idat + 8] = b"\xff\xff\xff\xff"
    shot = bytes(shot)
    assert ui_evidence.clip_to_max_height(shot) == (shot, False)


def test_tall_shot_is_cropped_exactly_as_before(monkeypatch):
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    for tall_shot in (rgba_png(3, 12), rgb_png(2, 11, (1, 2, 3))):
        width, _, rgba = ui_evidence.decode_png(tall_shot)
        want = ui_evidence.encode_png(width, 10, 6, rgba[:width * 10 * 4])
        assert ui_evidence.clip_to_max_height(tall_shot) == (want, True)


def _bad_header(kind: str) -> bytes:
    shot = bytearray(rgb_png(4, 4))
    if kind == "not-png":
        return b"definitely not a png, but long enough to have a header"
    if kind == "magic-only":
        return bytes(shot[:8])
    if kind == "truncated-ihdr":
        return bytes(shot[:20])
    if kind == "first-chunk-not-ihdr":
        shot[12:16] = b"tEXt"
        return bytes(shot)
    if kind == "ihdr-wrong-length":
        return bytes(shot[:8]) + ui_evidence._chunk(b"IHDR", b"\x00" * 14) + bytes(shot[33:])
    if kind == "short-ihdr-before-real-one":
        # Tall, so the shot needs cropping: decode_png alone would raise
        # struct.error on the 12-byte IHDR, which callers do not catch.
        tall = rgba_png(3, 12)
        return tall[:8] + ui_evidence._chunk(b"IHDR", b"\x00" * 12) + tall[8:]
    raise AssertionError(kind)


@pytest.mark.parametrize(
    "kind",
    [
        "not-png",
        "magic-only",
        "truncated-ihdr",
        "first-chunk-not-ihdr",
        "ihdr-wrong-length",
        "short-ihdr-before-real-one",
    ],
)
def test_clip_rejects_a_missing_or_malformed_header(monkeypatch, kind):
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    monkeypatch.setattr(ui_evidence, "decode_png", _no_decode)
    with pytest.raises(ValueError):
        ui_evidence.clip_to_max_height(_bad_header(kind))


def test_clip_names_an_ihdr_of_the_wrong_length():
    for kind in ("ihdr-wrong-length", "short-ihdr-before-real-one"):
        with pytest.raises(ValueError, match="wrong length"):
            ui_evidence.clip_to_max_height(_bad_header(kind))


@pytest.mark.parametrize("kind", ["palette", "sixteen-bit", "interlaced"])
def test_clip_still_rejects_unsupported_pngs(monkeypatch, kind):
    monkeypatch.setattr(ui_evidence, "decode_png", _no_decode)
    with pytest.raises(ValueError, match="unsupported PNG"):
        ui_evidence.clip_to_max_height(_unsupported_variant(kind))


def _with_chunk_before_ihdr(shot: bytes, chunk: bytes) -> bytes:
    return shot[:8] + chunk + shot[8:]


@pytest.mark.parametrize("first", ["text", "short-ihdr"])
def test_clip_sizes_non_conforming_pngs_like_decode_png(monkeypatch, first):
    # decode_png reads every chunk and the last IHDR wins; the header read
    # must pick the same size, so even a non-conforming PNG crops as before.
    monkeypatch.setattr(ui_evidence, "MAX_SHOT_HEIGHT_PX", 10)
    if first == "text":
        extra = ui_evidence._chunk(b"tEXt", b"Comment\x00example")
    else:
        extra = ui_evidence._chunk(b"IHDR", struct.pack(">IIBBBBB", 3, 5, 8, 2, 0, 0, 0))
    tall_shot = _with_chunk_before_ihdr(rgba_png(3, 12), extra)
    width, height, rgba = ui_evidence.decode_png(tall_shot)
    assert (width, height) == (3, 12)
    want = ui_evidence.encode_png(width, 10, 6, rgba[:width * 10 * 4])
    assert ui_evidence.clip_to_max_height(tall_shot) == (want, True)

# Publisher wiring: evidence-publish.yml validates with main's code --------------

# The privileged publisher runs scripts/publish_evidence.py from the default
# branch (never an inline copy, never PR code), so there is no inline
# validator to keep in sync. These tests pin the wiring instead: which
# script entry points the workflow calls, and that validation stays strict.


def test_publisher_renders_and_substitutes_with_main_scripts():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert "scripts/publish_evidence.py render" in workflow
    assert "scripts/publish_evidence.py substitute" in workflow
    assert "validate_artifact" in (ROOT / "scripts" / "publish_evidence.py").read_text()


def test_publisher_resolves_pr_from_event_not_artifact():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    # Fork PRs leave workflow_run.pull_requests empty, so the run looks the
    # PR up by head branch and verifies the head SHA through the API.
    assert "head_repository.owner.login" in workflow
    assert "pulls" in workflow
    assert ".head.sha" in workflow


def test_publisher_ignores_non_ci_workflow_runs():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    # A same-named workflow a PR adds must not trigger a publish.
    assert ".github/workflows/ci.yml" in workflow
    assert "workflows: [CI]" in workflow


def test_publisher_still_publishes_failed_runs():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert '"failure"' in workflow
    assert '"success"' in workflow


def test_publisher_serialises_runs_per_pr_branch():
    # Two runs for one PR must never overlap: the older run finishing last
    # would overwrite the newer run's comment and images. The key is the
    # head owner plus the head branch (fork-safe, from the trusted event),
    # and runs queue (no cancel-in-progress) so the newest run acts last.
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert (
        "evidence-publish-${{ github.event.workflow_run.head_repository.owner.login }}"
        in workflow
    )
    assert "github.event.workflow_run.head_branch" in workflow
    assert "cancel-in-progress: true" not in workflow


def test_publisher_rechecks_freshness_before_writing():
    # The head SHA is verified in resolve, then re-checked immediately
    # before pushing images and again immediately before posting: a moved
    # head stops the run without writing, and the newer run publishes.
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert workflow.count("stopping without writing") == 2
    assert 'gh api "repos/$REPO/pulls/$PR" --jq .head.sha' in workflow


def test_publisher_skips_push_when_no_pngs():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert "no PNGs to push for this head SHA" in workflow


def test_upload_warns_on_missing_files():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "if-no-files-found: warn" in workflow


def test_ci_never_writes_or_comments():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    # Posting and pushing moved to evidence-publish.yml (D16): the
    # unprivileged CI workflow only reads, uploads, and reports.
    assert "pull-requests: write" not in workflow
    assert "contents: write" not in workflow
    assert "evidence-comment" not in workflow


def test_evidence_job_is_text_only():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    # The Web UI section is rebuilt by the publisher from the validated
    # manifest; PR-built markup never reaches the comment.
    assert "--ui-artifacts" not in workflow


def test_workflows_pin_actions_by_sha():
    import re

    pinned = re.compile(r"^\s*- uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40} # ", re.M)
    for name in ("ci.yml", "ci-guard.yml", "evidence-publish.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text()
        uses = [line for line in workflow.splitlines() if "- uses:" in line]
        assert uses, name
        for line in uses:
            assert pinned.match(line + "\n"), f"{name}: unpinned action: {line.strip()}"


def test_no_caches_in_privileged_workflows():
    for name in ("ci-guard.yml", "evidence-publish.yml"):
        workflow = (ROOT / ".github" / "workflows" / name).read_text()
        assert "actions/cache" not in workflow, name


def test_validator_rejects_too_many_files(tmp_path, monkeypatch):
    directory = write_valid_artifact(tmp_path / "many")
    assert len(list(directory.iterdir())) == 2
    monkeypatch.setattr(ui_evidence, "MAX_FILE_COUNT", 1)
    errors = ui_evidence.validate_artifact(directory)
    assert any("over the 1 cap" in e for e in errors)


def test_validator_rejects_an_oversized_manifest(tmp_path, monkeypatch):
    directory = write_valid_artifact(tmp_path / "bigmanifest")
    monkeypatch.setattr(ui_evidence, "MANIFEST_MAX_BYTES", 10)
    errors = ui_evidence.validate_artifact(directory)
    assert any("over the 10 cap" in e for e in errors)


# End to end (needs Chromium) ---------------------------------------------------


@needs_browser
@pytest.mark.skipif(
    not (ROOT / "web" / "index.html").is_file(), reason="needs the dashboard in web/"
)
def test_capture_head_without_base_web(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    out = tmp_path / "out"
    rc = ui_evidence.main(
        [
            "--base-tree", str(base),
            "--head-tree", str(ROOT),
            "--out-dir", str(out),
            "--repo", REPO,
            "--pr", "7",
            "--head-sha", HEAD_SHA,
        ]
    )
    assert rc == 0
    manifest = json.loads((out / ui_evidence.MANIFEST_NAME).read_text())
    assert len(manifest["captures"]) == 28
    assert {c["status"] for c in manifest["captures"]} == {"added"}
    assert all(c["head_problems"] == [] for c in manifest["captures"])
    assert (out / ui_evidence.SECTION_NAME).read_text().startswith("## Web UI")
    assert ui_evidence.validate_artifact(out) != []  # .md files must not be pushed
    pushable = tmp_path / "push"
    pushable.mkdir()
    for path in out.iterdir():
        if path.suffix == ".png" or path.name == ui_evidence.MANIFEST_NAME:
            pushable.joinpath(path.name).write_bytes(path.read_bytes())
    assert ui_evidence.validate_artifact(pushable) == []


def test_gh_api_calls_with_fields_say_get_or_an_explicit_method():
    # gh api switches to POST whenever -f/-F fields are passed; a lookup that
    # forgets --method GET silently becomes a create request (and 403s).
    for name in ("ci.yml", "ci-guard.yml", "evidence-publish.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text()
        joined = re.sub(r"\\\n\s*", " ", text)
        for line in joined.splitlines():
            if "gh api" in line and re.search(r"\s-[fF]\s", line):
                assert re.search(r"(--method|-X)\s+[A-Z]+", line), (name, line.strip())


def test_evidence_job_uses_the_runners_chrome():
    # Playwright's bundled browsers only support some Ubuntu releases (1.60
    # refuses ubuntu-26.04); the runner's own Chrome has no such tie.
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    job = text[text.index("\n  evidence:\n"):]
    assert "playwright install" not in job
    assert "--browser-channel chrome" in job


def test_publisher_authenticates_git_the_way_github_accepts():
    # GitHub's git server rejects "Authorization: Bearer <token>" (the first
    # real publish failed with "could not read Username"); it takes Basic
    # auth with the x-access-token user, as actions/checkout sends it. The
    # encoded value is a new secret string, so it must be masked in logs.
    text = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert "Authorization: Bearer" not in text
    assert "x-access-token:" in text
    assert "Authorization: Basic ${" in text or 'Authorization: Basic $' in text
    assert "::add-mask::" in text


def test_publisher_does_not_hide_fetch_failures():
    # A failed fetch used to be read as "no pr-evidence branch yet", which
    # hid the real auth error behind a later, vaguer push failure.
    text = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert "pr-evidence 2>/dev/null" not in text
    assert "git ls-remote --exit-code --heads origin pr-evidence" in text


# Browser provenance (issue #8) -------------------------------------------------
#
# CI captures with the runner's Google Chrome, local runs with Playwright's
# bundled Chromium. The manifest records which browser captured, and both
# comment renderers name it; the publisher treats the key as hostile data.


INTRO_SENTENCE = (
    "Fixture house only. `scripts/ui_evidence.py` captures every fixture in "
    "`tests/fixtures/dashboard/` at phone (360x780), desktop (1280x900, light "
    "and dark), and e-ink (800x480) sizes."
)
IMAGES_SENTENCE = (
    "Images live on the `pr-evidence` branch, pinned to the evidence push, "
    "so they never change afterwards."
)
BROWSER_UNKNOWN = "The manifest does not record a recognised browser."


def test_browser_info_maps_no_channel_to_bundled_chromium():
    assert ui_evidence.browser_info(None, "141.0.7390") == {
        "channel": "chromium",
        "version": "141.0.7390",
    }
    assert ui_evidence.browser_info("chrome", "131.0.6778") == {
        "channel": "chrome",
        "version": "131.0.6778",
    }


def test_build_manifest_records_the_capturing_browser(tmp_path):
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    browser = {"channel": "chrome", "version": "131.0.6778"}
    manifest = ui_evidence.build_manifest(base, head, out, {}, browser=browser)
    assert manifest["version"] == 1
    assert manifest["browser"] == browser


def test_build_manifest_without_a_browser_records_null(tmp_path):
    base, head = _manifest_trees(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    assert ui_evidence.build_manifest(base, head, out, {})["browser"] is None


def _web_tree(root: Path, name: str, with_web: bool) -> Path:
    tree = root / name
    if with_web:
        (tree / "web").mkdir(parents=True)
        (tree / "web" / "index.html").write_text("<html></html>")
        (tree / "web" / "eink.html").write_text("<html></html>")
    else:
        tree.mkdir(parents=True)
    return tree


HEAD_BROWSER = {"channel": "chrome", "version": "131.0.6778"}
BASE_BROWSER = {"channel": "chromium", "version": "141.0.7390"}


@pytest.mark.parametrize(
    "base_web,head_web,want",
    [
        (True, True, HEAD_BROWSER),
        (False, True, HEAD_BROWSER),
        (True, False, BASE_BROWSER),
        (False, False, None),
    ],
)
def test_main_records_the_browser_that_captured(tmp_path, monkeypatch, base_web, head_web, want):
    # No browser: fake the Playwright import main checks for, and capture_side
    # itself. The manifest takes the head side's browser, else the base's,
    # else null when neither side captured.
    import sys
    import types

    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: None
    playwright.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delenv("HOUSESITTER_LIVE", raising=False)
    monkeypatch.delenv("HOUSESITTER_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    calls = []

    def fake_capture(tree, out_dir, side, check, channel=None):
        calls.append((side, channel))
        browser = HEAD_BROWSER if side == "head" else BASE_BROWSER
        return {}, dict(browser)

    monkeypatch.setattr(ui_evidence, "capture_side", fake_capture)
    base = _web_tree(tmp_path, "base", base_web)
    head = _web_tree(tmp_path, "head", head_web)
    out = tmp_path / "out"
    rc = ui_evidence.main(
        [
            "--base-tree", str(base),
            "--head-tree", str(head),
            "--out-dir", str(out),
            "--repo", REPO,
            "--pr", "7",
            "--head-sha", HEAD_SHA,
            "--browser-channel", "chrome",
        ]
    )
    assert rc == 0
    assert [side for side, _ in calls] == (
        (["base"] if base_web else []) + (["head"] if head_web else [])
    )
    assert {channel for _, channel in calls} <= {"chrome"}
    manifest = json.loads((out / ui_evidence.MANIFEST_NAME).read_text())
    assert manifest["version"] == 1
    assert manifest["browser"] == want


def test_validator_ignores_the_browser_key(tmp_path):
    directory = write_valid_artifact(tmp_path / "browserkey")
    manifest = json.loads((directory / ui_evidence.MANIFEST_NAME).read_text())
    manifest["browser"] = {"channel": "chrome", "version": "131.0.6778"}
    (directory / ui_evidence.MANIFEST_NAME).write_text(json.dumps(manifest))
    assert ui_evidence.validate_artifact(directory) == []


def test_section_names_the_capturing_browser():
    manifest = make_manifest()
    manifest["browser"] = {"channel": "chrome", "version": "131.0.6778"}
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert INTRO_SENTENCE in full and INTRO_SENTENCE in table
    captured = "Captured headless with Google Chrome 131.0.6778. " + IMAGES_SENTENCE
    assert captured in full and captured in table
    assert BROWSER_UNKNOWN not in full


def test_section_names_bundled_chromium_for_the_default_channel():
    manifest = make_manifest()
    manifest["browser"] = {"channel": "chromium", "version": "141.0.7390"}
    full, _ = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert "Captured headless with Playwright's bundled Chromium 141.0.7390." in full


def test_section_without_a_browser_key_is_neutral():
    # An old manifest has no "browser" key: the section still renders, with
    # the neutral sentence instead of a browser claim.
    full, table = ui_evidence.build_ui_section(make_manifest(), REPO, 7, HEAD_SHA, 60_000, True)
    assert BROWSER_UNKNOWN in full and BROWSER_UNKNOWN in table
    assert IMAGES_SENTENCE in full
    assert "Captured headless" not in full
    assert "| `typical / index-phone` | added | pass |" in table


HOSTILE_BROWSERS = [
    {"channel": "chrome<script>", "version": "131.0.6778"},
    {"channel": "chrome", "version": "1.2.3.4.5"},
    {"channel": "chrome", "version": "9" * 40},
    {"channel": "chrome", "version": 131},
    {"channel": "chrome"},
    {"channel": ["chrome"], "version": "131.0.6778"},
    {"channel": "firefox", "version": "131.0.6778"},
    ["chrome", "131.0.6778"],
    "chrome",
]


@pytest.mark.parametrize("browser", HOSTILE_BROWSERS)
def test_section_never_echoes_a_hostile_browser(browser):
    manifest = make_manifest()
    manifest["browser"] = browser
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert BROWSER_UNKNOWN in full and BROWSER_UNKNOWN in table
    assert "Captured headless" not in full
    if isinstance(browser, dict):
        for value in browser.values():
            if isinstance(value, str):
                assert value not in full and value not in table
    elif isinstance(browser, str):
        assert browser not in full


def test_section_without_captures_has_no_table_header():
    manifest = {"version": 1, "browser": None, "captures": []}
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, False)
    assert "nothing to capture" in full and "nothing to capture" in table
    assert "| Capture | Status | Checks |" not in full
    assert "| Capture | Status | Checks |" not in table
    assert "Captured headless" not in full
    assert IMAGES_SENTENCE not in full
    assert BROWSER_UNKNOWN not in full


@pytest.mark.parametrize(
    "channel, version, shown",
    [
        ("chrome", "131.0.6778.85", "Google Chrome 131.0.6778.85"),
        ("chrome-beta", "132.0.6834", "Google Chrome Beta 132.0.6834"),
        ("msedge", "131.0.2903", "Microsoft Edge 131.0.2903"),
        ("chrome", "131", "Google Chrome 131"),
        ("chrome", "9" * 32, "Google Chrome " + "9" * 32),
    ],
)
def test_section_names_every_allow_listed_browser(channel, version, shown):
    manifest = make_manifest()
    manifest["browser"] = {"channel": channel, "version": version}
    full, _ = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert f"Captured headless with {shown}. " + IMAGES_SENTENCE in full


@pytest.mark.parametrize(
    "browser",
    [None, {"channel": "chrome", "version": "9" * 33}, ["chrome", "131.0.6778"]],
)
def test_section_with_an_unusable_browser_keeps_its_rows(browser):
    manifest = make_manifest()
    manifest["browser"] = browser
    full, table = ui_evidence.build_ui_section(manifest, REPO, 7, HEAD_SHA, 60_000, True)
    assert BROWSER_UNKNOWN + " " + IMAGES_SENTENCE in full
    assert BROWSER_UNKNOWN in table
    assert "9" * 33 not in full and "131.0.6778" not in full
    assert "| Capture | Status | Checks |" in table
    assert "`typical / index-phone`" in table


# Removed captures (issue #21) ------------------------------------------------
#
# A removed capture keeps its base PNG on the pr-evidence branch, so the
# comment shows the before image with no after or diff image.


REMOVED_SENTENCE = "Removed page: no after image (the head no longer has this page)."
ADDED_SENTENCE = "New page: no before image (the base doesn't have this page)."


def _removed_manifest():
    return {
        "version": 1,
        "captures": [
            {
                "fixture": "typical",
                "view": "index-phone",
                "status": "removed",
                "base": "typical__index-phone__base.png",
                "head": None,
                "diff": None,
                "head_problems": [],
            }
        ],
    }


def test_section_shows_removed_capture_with_before_image_only():
    full, table = ui_evidence.build_ui_section(
        _removed_manifest(), REPO, 7, HEAD_SHA, 60_000, True
    )
    assert "| `typical / index-phone` | removed | pass |" in table
    assert "<details><summary>typical</summary>" in full
    assert REMOVED_SENTENCE in full
    assert "typical__index-phone__base.png" in full
    assert "after: typical / index-phone" not in full
    assert "diff: typical / index-phone" not in full
    assert "Diff unavailable" not in full


def test_section_with_only_unchanged_captures_has_no_details():
    full, table = ui_evidence.build_ui_section(
        make_manifest(status="unchanged"), REPO, 7, HEAD_SHA, 60_000, True
    )
    assert "| `typical / index-phone` | unchanged |" in table
    assert "<details>" not in full


def test_added_sentence_names_the_page_not_the_web_dir():
    full, _ = ui_evidence.build_ui_section(
        make_manifest(), REPO, 7, HEAD_SHA, 60_000, True
    )
    assert ADDED_SENTENCE in full
    assert "the base has no `web/`" not in full
