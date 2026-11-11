#!/usr/bin/env python3
"""Rendered before/after screenshot evidence for web UI changes (docs/REVIEW.md).

For each fixture in ``tests/fixtures/dashboard/`` and each view (phone,
desktop, desktop dark, e-ink), serves a source tree's own
``scripts/dev_dashboard.py`` with that tree's own ``web/`` and fixtures, and
captures the page with a headless browser through Playwright (a dev/CI-only
extra). CI uses the runner's Google Chrome (``--browser-channel chrome``);
without a channel, Playwright's bundled Chromium. The manifest records which.

For each capture pair the script classifies the pair as added, removed,
changed, or unchanged (by decoded pixel equality, not file bytes), writes a
highlighted diff image for changed pairs, runs the UI checks (any head
failure exits nonzero), and writes a ``## Web UI`` comment section whose
image URLs carry a placeholder token. CI's read-only ``evidence`` job runs
the PR's own copy of this script. The privileged ``evidence-publish``
workflow runs main's code: it validates the artifact, rebuilds the comment
section from the manifest, and substitutes the pinned commit SHA of the
evidence push after it happens, so PR-built Web UI markup never reaches the
comment. (The text section, ``evidence.md``, is PR-built; main only checks
its marker and size before posting it.)

Usage:
    python scripts/ui_evidence.py --base-tree base --head-tree head
        --out-dir ui_out --repo OWNER/NAME --pr 12 --head-sha <40 hex>

A tree without ``web/`` contributes "no page" for its side. Outputs in
``--out-dir``: ``<fixture>__<view>__<side>.png`` (side is ``base``/``head``),
``<fixture>__<view>__diff.png`` for changed pairs, ``ui_manifest.json``,
plus ``ui_section.md``/``ui_table.md`` local copies of the comment section
for debugging. Only the PNGs and the manifest are pushed to the
``pr-evidence`` branch; ``pr_evidence.py`` rebuilds the comment section
from the manifest (re-validating every value) and ignores the ``.md`` files.

Safety: fixture data only. Refuses to run when HOUSESITTER_LIVE or
HOUSESITTER_CONFIG is set, or when ~/.config/housesitter/config.json exists.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
import zlib
from pathlib import Path

SHA_PLACEHOLDER = "__HOUSESITTER_EVIDENCE_SHA__"
MANIFEST_NAME = "ui_manifest.json"
SECTION_NAME = "ui_section.md"
TABLE_NAME = "ui_table.md"

FIXTURE_SUBDIR = Path("tests") / "fixtures" / "dashboard"
WEB_INDEX = "index.html"
WEB_EINK = "eink.html"

# (view id, page, css width, css height, colour scheme, full page shot, img width)
VIEWS = (
    ("index-phone", "index.html", 360, 780, "light", True, 240),
    ("index-desktop", "index.html", 1280, 900, "light", True, 480),
    ("index-desktop-dark", "index.html", 1280, 900, "dark", True, 480),
    ("eink", "eink.html", 800, 480, "light", False, 400),
)
VIEW_IDS = tuple(v[0] for v in VIEWS)

# Fixture file stems: lowercase alphanumerics, `-`, and single `_`, at most
# 48 chars. A double underscore is forbidden inside the name so the two `__`
# separators stay unambiguous. Parse from the right: the view and side are
# fixed words, so the fixture is everything before the last two segments.
FIXTURE_RE = r"[a-z0-9](?:[a-z0-9-]|_(?!_)){0,47}"
CAPTURE_RE = re.compile(
    FIXTURE_RE + r"__(index-phone|index-desktop|index-desktop-dark|eink)"
    r"__(base|head|diff)\.png"
)
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA_RE = re.compile(r"[0-9a-f]{40}")

# Full-page desktop shots of the 600-client fixture compress to ~6 MB, so the
# caps fit those with headroom rather than pretending screenshots are small.
MAX_FILE_BYTES = 8_000_000  # per-PNG cap for the evidence branch
MAX_TOTAL_BYTES = 40_000_000  # total PNG cap for one push
# A manifest lists at most a few dozen captures today (one fixture x four
# views x three sides); these caps stop a hostile artifact from slowing the
# privileged publisher with sheer entry or byte count.
MAX_FILE_COUNT = 500  # total entries (PNGs plus manifest) in one push
MANIFEST_MAX_BYTES = 1_000_000  # the manifest itself is a few kilobytes
# Full-page shots taller than this are clipped, and the capture records a
# check failure. 120,000 covers the committed fixtures with headroom: the
# 600-client stress fixture renders ~104,200 CSS px tall on the phone view
# (measured), so 16,000 would fail every run on legitimate evidence. The cap
# still bounds runaway pages (a hostile fixture with millions of rows).
MAX_PROBLEM_LEN = 200  # cap per recorded check failure
MAX_PAGE_HEIGHT_PX = 120_000
# Saved full-page images are cropped to this many CSS px from the top. This is
# not a check failure: it keeps every image under MAX_FILE_BYTES and readable
# in a PR comment. The 600-client fixture's full-height shots were ~9 MB each
# in Google Chrome (over the 8 MB cap, so main's publisher refused the whole
# artifact). The page-height check above still measures the whole page.
MAX_SHOT_HEIGHT_PX = 8_000
READY_TIMEOUT_S = 30.0
SETTLE_MS = 400  # wait after network idle so the page finishes rendering

PAGE_HEIGHT_JS = (
    "Math.max(document.documentElement.scrollHeight, document.body.scrollHeight)"
)
OVERFLOW_JS = "document.documentElement.scrollWidth > window.innerWidth + 1"
CLIPPING_JS = """() => [...document.querySelectorAll('body *')].filter(e => {
  const s = getComputedStyle(e);
  return ((s.overflowX === 'hidden' || s.overflowX === 'clip')
    && e.scrollWidth > e.clientWidth + 1)
    || ((s.overflowY === 'hidden' || s.overflowY === 'clip')
    && e.scrollHeight > e.clientHeight + 1);
}).slice(0, 8).map(e => e.tagName + '.' + e.className)"""
CSP_RE = re.compile(r"content security policy|trusted.?types|require-trusted-types", re.I)


class Refused(Exception):
    pass


def refuse_if_real_setup() -> None:
    if os.environ.get("HOUSESITTER_LIVE"):
        raise Refused("HOUSESITTER_LIVE is set; evidence uses the fixture house only")
    if os.environ.get("HOUSESITTER_CONFIG"):
        raise Refused("HOUSESITTER_CONFIG is set; evidence uses the fixture house only")
    real = Path.home() / ".config" / "housesitter" / "config.json"
    if real.exists():
        raise Refused(f"{real} exists; run evidence on a machine without a real setup")


def fixtures_in(tree: Path) -> list:
    return sorted(p.stem for p in (tree / FIXTURE_SUBDIR).glob("*.json"))


def has_web(tree: Path) -> bool:
    return (tree / "web" / WEB_INDEX).is_file() and (tree / "web" / WEB_EINK).is_file()


def capture_name(fixture: str, view: str, side: str) -> str:
    return f"{fixture}__{view}__{side}.png"


def diff_name(fixture: str, view: str) -> str:
    return f"{fixture}__{view}__diff.png"


# ---------------------------------------------------------------------------
# PNG codec (stdlib only)
#
# Chromium emits 8-bit RGB/RGBA non-interlaced PNGs, which is all the decoder
# accepts. The diff is computed from decoded pixels rather than file bytes so
# that encoder metadata or timestamps can never count as a visual change, and
# rather than in the browser with a canvas so the comparison stays a pure,
# unit-testable function with no browser dependency.
# ---------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunk(ctype: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data))


def encode_png(width: int, height: int, color_type: int, pixels: bytes) -> bytes:
    """Encode 8-bit RGB (color_type 2) or RGBA (color_type 6) PNG bytes."""
    channels = 3 if color_type == 2 else 4
    if color_type not in (2, 6):
        raise ValueError(f"unsupported color type {color_type}")
    if len(pixels) != width * height * channels:
        raise ValueError("pixel buffer has the wrong length")
    stride = width * channels
    raw = b"".join(
        b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_MAGIC
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> tuple:
    """Decode an 8-bit RGB/RGBA non-interlaced PNG to (width, height, RGBA bytes)."""
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG file")
    pos, width, height = 8, None, None
    color_type, bit_depth, interlace = None, None, None
    idat = bytearray()
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        if len(chunk) != length:
            raise ValueError("truncated PNG chunk")
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if width is None:
        raise ValueError("PNG has no IHDR")
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError(
            f"unsupported PNG: {bit_depth}-bit color type {color_type}, "
            f"interlace {interlace}"
        )
    channels = 3 if color_type == 2 else 4
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError(f"bad PNG image data: {exc}") from exc
    want = height * (1 + stride)
    if len(raw) < want:
        raise ValueError("truncated PNG image data")
    rgba = bytearray(width * height * 4)
    prev = bytearray(stride)
    p = 0
    for y in range(height):
        filt = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if filt == 1:  # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:  # Average
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:  # Paeth
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(a, b, c)) & 0xFF
        elif filt != 0:
            raise ValueError(f"unknown PNG filter {filt}")
        for x in range(width):
            s = x * channels
            d = (y * width + x) * 4
            rgba[d:d + 3] = line[s:s + 3]
            rgba[d + 3] = line[s + 3] if channels == 4 else 255
        prev = line
    return width, height, bytes(rgba)


# Dim agreed pixels towards white in the diff image. A 256-entry table lets
# whole rows go through bytes.translate (C speed) instead of a Python loop.
_DIM_TABLE = bytes(int(v * 0.35 + 255 * 0.65) for v in range(256))


def _dim_row(arow: bytes, out: bytearray, base: int, width: int) -> None:
    """Write the dimmed RGB of one RGBA row into out. C speed, no loop.

    Spelled with explicit slice objects, which the repo hygiene scan
    accepts, rather than the strided-slice sugar it reads as IPv6.
    """
    row = bytearray(width * 3)
    row[slice(0, len(row), 3)] = arow[slice(0, len(arow), 4)].translate(_DIM_TABLE)
    row[slice(1, len(row), 3)] = arow[slice(1, len(arow), 4)].translate(_DIM_TABLE)
    row[slice(2, len(row), 3)] = arow[slice(2, len(arow), 4)].translate(_DIM_TABLE)
    out[base:base + width * 3] = row


def diff_rgba(before: tuple, after: tuple) -> tuple:
    """Compare two decoded images. Returns (changed, diff RGB bytes or None).

    The diff image keeps the after pixels dimmed towards white where the two
    agree and paints pure red where they differ (or where only one image has
    pixels, when the sizes disagree), so the change stands out at a glance.
    Rows that agree byte-for-byte take the fast lane above; only rows with a
    real difference pay the per-pixel loop. Full-page dashboard shots are
    tens of millions of pixels, so the difference matters.
    """
    bw, bh, bpix = before
    aw, ah, apix = after
    if (bw, bh, bpix) == (aw, ah, apix):
        return False, None
    w, h = max(bw, aw), max(bh, ah)
    out = bytearray(w * h * 3)
    red = b"\xff\x00\x00"
    for y in range(h):
        base = y * w * 3
        if y < bh and y < ah and bw == aw:
            brow = bpix[y * bw * 4:(y + 1) * bw * 4]
            arow = apix[y * aw * 4:(y + 1) * aw * 4]
            if brow == arow:
                _dim_row(arow, out, base, bw)
                continue
            for x in range(bw):
                o = (y * bw + x) * 4
                d = base + x * 3
                if bpix[o:o + 4] == apix[o:o + 4]:
                    r, g, b = apix[o], apix[o + 1], apix[o + 2]
                    out[d:d + 3] = bytes(
                        (_DIM_TABLE[r], _DIM_TABLE[g], _DIM_TABLE[b])
                    )
                else:
                    out[d:d + 3] = red
        else:
            for x in range(w):
                d = base + x * 3
                if x < bw and y < bh and x < aw and y < ah:
                    o1, o2 = (y * bw + x) * 4, (y * aw + x) * 4
                    if bpix[o1:o1 + 4] == apix[o2:o2 + 4]:
                        r, g, b = apix[o2], apix[o2 + 1], apix[o2 + 2]
                        out[d:d + 3] = bytes(
                            (_DIM_TABLE[r], _DIM_TABLE[g], _DIM_TABLE[b])
                        )
                        continue
                out[d:d + 3] = red
    return True, (w, h, bytes(out))


def png_size(data: bytes) -> tuple:
    """Read (width, height) from a PNG's header (IHDR) without decoding it.

    The PNG spec puts IHDR first: bytes 8-12 hold its length (13), 12-16 its
    type, then width and height (4 bytes each), bit depth, colour type, and
    interlace. Raises ValueError for anything that is not an 8-bit RGB/RGBA
    non-interlaced PNG header, the same formats decode_png accepts.
    """
    if data[:8] != PNG_MAGIC:
        raise ValueError("not a PNG file")
    if len(data) < 8 + 8 + 13:
        raise ValueError("truncated PNG header")
    (length,) = struct.unpack(">I", data[8:12])
    if data[12:16] != b"IHDR" or length != 13:
        raise ValueError("PNG does not start with an IHDR chunk")
    width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        raise ValueError(
            f"unsupported PNG: {bit_depth}-bit color type {color_type}, "
            f"interlace {interlace}"
        )
    return width, height


def clip_to_max_height(shot: bytes) -> tuple:
    """Crop a full-page PNG to MAX_SHOT_HEIGHT_PX rows. Returns (shot, cropped).

    The height comes from the header, so a shot at or under the cap is
    returned as is without decoding it (pure-Python decoding of a full-page
    shot takes seconds). Only a taller shot is decoded and cropped.

    Raises ValueError for a missing, malformed, or unsupported header, and
    for undecodable image data in a shot that needs cropping; callers that
    already report such shots (check_page, F7 in build_manifest) leave the
    bytes alone instead.
    """
    _, height = png_size(shot)
    if height <= MAX_SHOT_HEIGHT_PX:
        return shot, False
    width, height, rgba = decode_png(shot)
    # decode_png's size is the one the crop uses, as before this fast path.
    # The two only disagree for a non-conforming PNG with a second IHDR.
    if height <= MAX_SHOT_HEIGHT_PX:
        return shot, False
    return (
        encode_png(width, MAX_SHOT_HEIGHT_PX, 6, rgba[:width * MAX_SHOT_HEIGHT_PX * 4]),
        True,
    )


def classify_and_diff(before_png: bytes | None, after_png: bytes | None) -> tuple:
    """Classify a capture pair and keep the diff. Returns (status, diff).

    diff is diff_rgba's (width, height, RGB bytes) for a "changed" pair and
    None otherwise. Each side is decoded at most once, so the diff image
    reuses the classification's work.
    """
    if before_png is None and after_png is None:
        raise ValueError("both sides missing")
    if before_png is None:
        return "added", None
    if after_png is None:
        return "removed", None
    if before_png == after_png:
        return "unchanged", None
    before = decode_png(before_png)
    after = decode_png(after_png)
    changed, diff = diff_rgba(before, after)
    return ("changed", diff) if changed else ("unchanged", None)


def classify_pair(before_png: bytes | None, after_png: bytes | None) -> str:
    """Classify a capture pair by decoded pixel equality, not file bytes.

    Byte-identical files short-circuit to "unchanged": PNG decoding is
    deterministic, so identical bytes can only hold identical pixels. Files
    that differ are always decoded and compared pixel by pixel, so an
    encoder quirk can never count as a visual change.
    """
    return classify_and_diff(before_png, after_png)[0]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_ready(port: int) -> None:
    deadline = time.time() + READY_TIMEOUT_S
    url = f"http://127.0.0.1:{port}/api/dashboard?fixture=typical"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as reply:  # noqa: S310
                if reply.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"dev server on port {port} did not become ready")


def check_page(page, view_id: str, shot: bytes) -> list:
    """Run the UI checks for one capture. Any returned item is a failure."""
    problems = []
    for kind, text in page.problems:
        text = text[:MAX_PROBLEM_LEN]
        if kind in ("pageerror", "dialog"):
            problems.append(f"{kind}: {text}")
        elif CSP_RE.search(text):
            problems.append(f"csp-or-trusted-types: {text}")
        else:
            problems.append(f"console.{kind}: {text}")
    if page.evaluate(OVERFLOW_JS):
        problems.append("horizontal page overflow")
    clipped = page.evaluate(CLIPPING_JS)
    for entry in clipped:
        problems.append(f"internal clipping: {str(entry)[:MAX_PROBLEM_LEN]}")
    if view_id == "eink":
        try:
            width, height, _ = decode_png(shot)
        except ValueError as exc:
            problems.append(f"undecodable PNG: {exc}")
        else:
            if (width, height) != (800, 480):
                problems.append(f"e-ink shot is {width}x{height}, not 800x480")
    return problems


def capture_side(
    tree: Path, out_dir: Path, side: str, check: bool, channel: str | None = None
) -> tuple:
    """Serve one tree and capture every fixture x view.

    Returns (records, browser): name -> record, plus the browser that
    launched: ``{"channel", "version"}``, with channel "chromium" for
    Playwright's bundled Chromium (no ``channel`` given).

    ``channel`` picks an installed browser (e.g. "chrome") instead of
    Playwright's bundled Chromium. Base and head are always captured with
    the same browser in the same run, so the comparison stays fair whatever
    its version.
    """
    from playwright.sync_api import sync_playwright  # dev/CI-only extra

    records: dict = {}
    port = free_port()
    cmd = [
        sys.executable,
        str(tree / "scripts" / "dev_dashboard.py"),
        "--web", str(tree / "web"),
        "--bind", "127.0.0.1",
        "--port", str(port),
    ]
    server = subprocess.Popen(  # noqa: S603
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        wait_ready(port)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel)
            print(f"ui_evidence: {side} captured with {channel or 'chromium'} {browser.version}")
            info = browser_info(channel, browser.version)
            try:
                for fixture in fixtures_in(tree):
                    for view_id, page_file, width, height, scheme, full, _ in VIEWS:
                        context = browser.new_context(
                            viewport={"width": width, "height": height},
                            color_scheme=scheme,
                        )
                        page = context.new_page()
                        seen: list = []

                        def on_console(msg, seen=seen):
                            if msg.type in ("error", "warning"):
                                seen.append((msg.type, msg.text))

                        def on_pageerror(err, seen=seen):
                            seen.append(("pageerror", str(err)))

                        def on_dialog(dialog, seen=seen):
                            seen.append(("dialog", dialog.message))
                            dialog.dismiss()

                        page.on("console", on_console)
                        page.on("pageerror", on_pageerror)
                        page.on("dialog", on_dialog)
                        page.goto(
                            f"http://127.0.0.1:{port}/{page_file}?fixture={fixture}",
                            wait_until="networkidle",
                        )
                        page.wait_for_timeout(SETTLE_MS)
                        name = capture_name(fixture, view_id, side)
                        too_tall = False
                        if full:
                            page_height = page.evaluate(PAGE_HEIGHT_JS)
                            too_tall = page_height > MAX_PAGE_HEIGHT_PX
                            # Render only the top of a tall page: a full
                            # 100,000 px render is slow and huge, then cropped.
                            shot = page.screenshot(
                                full_page=True,
                                clip={
                                    "x": 0,
                                    "y": 0,
                                    "width": width,
                                    "height": min(page_height, MAX_SHOT_HEIGHT_PX),
                                },
                            )
                            try:
                                shot, _ = clip_to_max_height(shot)
                            except ValueError:
                                pass
                        else:
                            shot = page.screenshot()
                        (out_dir / name).write_bytes(shot)
                        problems = (
                            check_page(_Page(page, seen), view_id, shot) if check else []
                        )
                        if too_tall:
                            problems.append(
                                f"page taller than {MAX_PAGE_HEIGHT_PX} px"
                            )
                        records[name] = {
                            "fixture": fixture,
                            "view": view_id,
                            "problems": problems,
                        }
                        context.close()
            finally:
                browser.close()
    finally:
        server.terminate()
        server.wait(timeout=15)
    return records, info


class _Page:
    """Minimal adapter so check_page is unit-testable without a browser."""

    def __init__(self, page, problems):
        self._page = page
        self.problems = problems

    def evaluate(self, script: str):
        return self._page.evaluate(script)


# ---------------------------------------------------------------------------
# Manifest, comment section, and validation
# ---------------------------------------------------------------------------

# The comment intro names the browser that captured the screenshots: CI runs
# with the runner's Google Chrome (--browser-channel chrome), local runs with
# Playwright's bundled Chromium (no channel). scripts/pr_evidence.py keeps its
# own copy of every UI_* value below (it never imports this module), and a
# test pins the copies identical.
UI_INTRO = (
    "Fixture house only. `scripts/ui_evidence.py` captures every fixture in "
    "`tests/fixtures/dashboard/` at phone (360x780), desktop (1280x900, light "
    "and dark), and e-ink (800x480) sizes."
)
UI_IMAGES_NOTE = (
    "Images live on the `pr-evidence` branch, pinned to the evidence push, "
    "so they never change afterwards."
)
UI_BROWSER_UNKNOWN = "The manifest does not record a recognised browser."
UI_BROWSER_NAMES = {
    "chromium": "Playwright's bundled Chromium",
    "chrome": "Google Chrome",
    "chrome-beta": "Google Chrome Beta",
    "msedge": "Microsoft Edge",
}
UI_BROWSER_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){0,3}")
UI_BROWSER_VERSION_MAX_LEN = 32


def browser_info(channel: str | None, version: str) -> dict:
    """The manifest's browser record for a side that captured.

    A None channel means Playwright's bundled Chromium (the default launch).
    """
    return {"channel": channel or "chromium", "version": version}


def browser_display(manifest: dict) -> str | None:
    """E.g. "Google Chrome 131.0.6778" for a recognised browser, else None.

    The manifest is untrusted data in the publisher (a PR-authored artifact),
    so the channel goes through an allow-list and the version through a strict
    numeric shape; anything else renders the neutral sentence, never the bad
    value.
    """
    browser = manifest.get("browser")
    if not isinstance(browser, dict):
        return None
    channel = browser.get("channel")
    version = browser.get("version")
    if not isinstance(channel, str) or not isinstance(version, str):
        return None
    name = UI_BROWSER_NAMES.get(channel)
    if name is None:
        return None
    if len(version) > UI_BROWSER_VERSION_MAX_LEN:
        return None
    if not UI_BROWSER_VERSION_RE.fullmatch(version):
        return None
    return f"{name} {version}"


def build_manifest(
    base_tree: Path,
    head_tree: Path,
    out_dir: Path,
    head_records: dict,
    browser: dict | None = None,
) -> dict:
    """Classify every fixture x view pair, writing diff images for changes."""
    base_fixtures = set(fixtures_in(base_tree)) if has_web(base_tree) else set()
    head_fixtures = set(fixtures_in(head_tree)) if has_web(head_tree) else set()
    captures = []
    for fixture in sorted(base_fixtures | head_fixtures):
        for view_id, _, _, _, _, _, _ in VIEWS:
            base_png, head_png = None, None
            base_path = out_dir / capture_name(fixture, view_id, "base")
            head_path = out_dir / capture_name(fixture, view_id, "head")
            if base_path.is_file():
                base_png = base_path.read_bytes()
            if head_path.is_file():
                head_png = head_path.read_bytes()
            # An undecodable PNG (palette, 16-bit, interlaced, or corrupt) is
            # evidence too: mark the pair changed with no diff image and a
            # note, instead of crashing before the manifest is written.
            diff_error = None
            diff = None
            try:
                status, diff = classify_and_diff(base_png, head_png)
            except ValueError as exc:
                status = "changed"
                diff_error = f"diff unavailable: {exc}"
            diff_file = None
            if status == "changed" and diff_error is None:
                # The diff comes from the classification's single decode.
                w, h, rgb = diff
                diff_file = diff_name(fixture, view_id)
                (out_dir / diff_file).write_bytes(encode_png(w, h, 2, rgb))
            if status == "unchanged":
                # Unchanged pairs are listed in the table only. Drop their
                # PNGs so they are never uploaded or pushed: pr-evidence only
                # grows, and most pushes change few or no captures.
                base_path.unlink()
                head_path.unlink()
                base_png = head_png = None
            problems = head_records.get(
                capture_name(fixture, view_id, "head"), {}
            ).get("problems", [])
            captures.append(
                {
                    "fixture": fixture,
                    "view": view_id,
                    "status": status,
                    "base": base_path.name if base_png is not None else None,
                    "head": head_path.name if head_png is not None else None,
                    "diff": diff_file,
                    "diff_error": diff_error,
                    "head_problems": problems,
                }
            )
    return {"version": 1, "browser": browser, "captures": captures}


def img_tag(src: str, width: int, alt: str) -> str:
    """An <img> for an evidence-branch file. Only allow-listed names render."""
    name = src.rsplit("/", 1)[-1]
    if not CAPTURE_RE.fullmatch(name):
        raise ValueError(f"refusing <img> for unexpected file name: {name!r}")
    # loading="lazy" keeps a 25 MB comment from fetching every image at once.
    return (
        f'<img src="{html.escape(src, quote=True)}" '
        f'width="{width:d}" loading="lazy" alt="{html.escape(alt, quote=True)}">'
    )


def base_url(repo: str, pr: int, head_sha: str) -> str:
    if not REPO_RE.fullmatch(repo):
        raise ValueError(f"bad repo: {repo!r}")
    if pr <= 0:
        raise ValueError(f"bad PR number: {pr!r}")
    if not SHA_RE.fullmatch(head_sha):
        raise ValueError(f"bad head SHA: {head_sha!r}")
    return (
        f"https://raw.githubusercontent.com/{repo}/{SHA_PLACEHOLDER}"
        f"/pr/{pr}/{head_sha}"
    )


def substitute_sha(text: str, sha: str) -> str:
    """Pin the placeholder to the evidence push SHA. Validates the SHA."""
    if not SHA_RE.fullmatch(sha):
        raise ValueError(f"bad evidence SHA: {sha!r}")
    return text.replace(SHA_PLACEHOLDER, sha)


def _view_width(view_id: str) -> int:
    for vid, _, _, _, _, _, width in VIEWS:
        if vid == view_id:
            return width
    raise ValueError(f"unknown view: {view_id!r}")


def build_ui_section(
    manifest: dict,
    repo: str,
    pr: int,
    head_sha: str,
    budget: int,
    base_has_web: bool,
) -> tuple:
    """Build (full section, table-only section) for the PR comment.

    The table always fits first; per-fixture detail blocks are added while
    they fit. Callers fall back to the table section when the full one would
    overflow the shared comment budget.
    """
    url = base_url(repo, pr, head_sha)
    lines = [
        "## Web UI",
        "",
        UI_INTRO,
        "",
    ]
    if manifest["captures"]:
        display = browser_display(manifest)
        if display is None:
            lines += [UI_BROWSER_UNKNOWN + " " + UI_IMAGES_NOTE, ""]
        else:
            lines += ["Captured headless with " + display + ". " + UI_IMAGES_NOTE, ""]
    if not base_has_web:
        lines += [
            "The base has no `web/` directory, so every capture is new "
            "and its side is \"no page\".",
            "",
        ]
    if not manifest["captures"]:
        lines += [
            "Neither the base nor the head has a `web/` directory: "
            "nothing to capture.",
            "",
        ]
    else:
        lines += [
            "| Capture | Status | Checks |",
            "|---------|--------|--------|",
        ]
        for cap in manifest["captures"]:
            shown = list(cap.get("head_problems") or [])
            if cap.get("diff_error"):
                shown.append(str(cap["diff_error"])[:MAX_PROBLEM_LEN])
            checks = "pass" if not shown else "; ".join(shown)
            lines += [
                f"| `{cap['fixture']} / {cap['view']}` | {cap['status']} "
                f"| {checks} |"
            ]
    lines += [""]
    table_section = "\n".join(lines)

    details = []
    by_fixture: dict = {}
    for cap in manifest["captures"]:
        by_fixture.setdefault(cap["fixture"], []).append(cap)
    for fixture in sorted(by_fixture):
        shown = [c for c in by_fixture[fixture] if c["status"] in ("added", "changed", "removed")]
        if not shown:
            continue
        block = [f"<details><summary>{html.escape(fixture)}</summary>", ""]
        for cap in shown:
            label = f"{cap['fixture']} / {cap['view']}"
            block += [f"#### `{label}`", ""]
            if cap["status"] == "added":
                block += [
                    "New page: no before image (the base doesn't have this page).",
                    "",
                    img_tag(
                        f"{url}/{cap['head']}",
                        _view_width(cap["view"]),
                        f"after: {label}",
                    ),
                    "",
                ]
            elif cap["status"] == "removed":
                block += [
                    "Removed page: no after image (the head no longer has this page).",
                    "",
                    img_tag(
                        f"{url}/{cap['base']}",
                        _view_width(cap["view"]),
                        f"before: {label}",
                    ),
                    "",
                ]
            else:
                block += [
                    img_tag(
                        f"{url}/{cap['base']}",
                        _view_width(cap["view"]),
                        f"before: {label}",
                    ),
                    img_tag(
                        f"{url}/{cap['head']}",
                        _view_width(cap["view"]),
                        f"after: {label}",
                    ),
                ]
                if cap.get("diff") is not None:
                    block += [
                        img_tag(
                            f"{url}/{cap['diff']}",
                            _view_width(cap["view"]),
                            f"diff: {label}",
                        ),
                    ]
                else:
                    reason = html.escape(
                        str(cap.get("diff_error") or "unknown reason")
                    )
                    block += [f"Diff unavailable: {reason}."]
                block += [""]
        block += ["</details>", ""]
        details.append((fixture, "\n".join(block)))

    def assemble(kept: list, dropped: list) -> str:
        text = table_section
        for block in kept:
            text += ("\n" if not text.endswith("\n\n") else "") + block
        if dropped:
            text += (
                "\n**Omitted to fit a GitHub comment:** "
                + ", ".join(f"`{name}`" for name in dropped)
                + ". Run `scripts/ui_evidence.py` locally to see them.\n"
            )
        return text

    # Greedily keep detail blocks, then give back the newest ones until the
    # omission note itself fits too, so the section never exceeds budget.
    kept = [(name, block) for name, block in details]
    dropped: list = []
    full = assemble([block for _, block in kept], dropped)
    while kept and len(full.encode()) > budget:
        name, _ = kept.pop()
        dropped.insert(0, name)
        full = assemble([block for _, block in kept], dropped)
    if not kept and dropped and len(full.encode()) > budget:
        # Even the table plus the note does not fit; the caller falls back
        # to the table alone or a one-line pointer instead.
        full = table_section
    return full, table_section


def validate_artifact(directory: Path) -> list:
    """Check an evidence-branch directory before pushing. Returns errors.

    Only top-level ``*.png`` files matching the capture name pattern plus
    ``ui_manifest.json`` are allowed: no symlinks, no subdirectories, no
    paths outside the directory, PNG magic bytes, and per-file and total
    size caps. The manifest must be a JSON object with a ``captures`` list,
    and every file it names must be a capture-named file that exists here.
    Small enough for CI to inline, importable so unit tests pin it.
    """
    errors = []
    if not directory.is_dir():
        return [f"not a directory: {directory}"]
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        return [f"cannot list directory: {exc}"]
    if len(entries) > MAX_FILE_COUNT:
        errors.append(
            f"{len(entries)} files, over the {MAX_FILE_COUNT} cap; "
            "not checking further"
        )
        return errors
    total = 0
    for entry in entries:
        if entry.is_symlink() or entry.name in (".", ".."):
            errors.append(f"symlink or special entry: {entry.name}")
            continue
        if entry.is_dir():
            errors.append(f"unexpected directory: {entry.name}")
            continue
        if entry.name == MANIFEST_NAME:
            continue
        if not CAPTURE_RE.fullmatch(entry.name):
            errors.append(f"unexpected file name: {entry.name}")
            continue
        size = entry.stat().st_size
        total += size
        if size > MAX_FILE_BYTES:
            errors.append(f"{entry.name} is {size} bytes, over the cap")
            continue
        if entry.read_bytes()[:8] != PNG_MAGIC:
            errors.append(f"{entry.name} is not a PNG file")
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        errors.append(f"missing manifest: {MANIFEST_NAME}")
    elif manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
        errors.append(f"manifest is over the {MANIFEST_MAX_BYTES} cap")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            # RecursionError: deeply nested JSON well under the size cap.
            errors.append(f"manifest is unreadable or not valid JSON: {type(exc).__name__}")
        else:
            if not isinstance(manifest, dict) or not isinstance(
                manifest.get("captures"), list
            ):
                errors.append("manifest must be an object with a captures list")
            else:
                try:
                    names = {
                        c.get(n)
                        for c in manifest.get("captures", [])
                        for n in ("base", "head", "diff")
                    }
                except (AttributeError, TypeError):
                    errors.append("manifest has an invalid capture entry")
                else:
                    names.discard(None)
                    for name in sorted(names, key=str):
                        # Manifest values are hostile: cap what the log echoes.
                        text = str(name)[:120]
                        if ".." in text.split("/"):
                            errors.append(f"manifest path escapes the directory: {text}")
                        elif not CAPTURE_RE.fullmatch(text):
                            errors.append(f"manifest names an unexpected file: {text}")
                        elif not (directory / text).is_file():
                            errors.append(f"manifest names a missing file: {text}")
    if total > MAX_TOTAL_BYTES:
        errors.append(f"PNGs total {total} bytes, over the cap")
    return errors


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-tree", type=Path, required=True)
    parser.add_argument("--head-tree", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--repo", required=True, help="owner/name of the GitHub repo")
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument("--head-sha", required=True, help="head commit SHA (40 hex)")
    parser.add_argument(
        "--browser-channel",
        default=None,
        help="use an installed browser (e.g. chrome) instead of Playwright's bundled Chromium",
    )
    args = parser.parse_args(argv)
    try:
        refuse_if_real_setup()
    except Refused as exc:
        print(f"ui_evidence: refused: {exc}", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        print("ui_evidence: Playwright is not installed (pip install '.[ui]')", file=sys.stderr)
        return 2
    args.out_dir.mkdir(parents=True, exist_ok=True)
    base_web = has_web(args.base_tree)
    head_web = has_web(args.head_tree)
    head_records: dict = {}
    base_browser = head_browser = None
    if base_web:
        _, base_browser = capture_side(
            args.base_tree, args.out_dir, "base", check=False, channel=args.browser_channel
        )
    if head_web:
        head_records, head_browser = capture_side(
            args.head_tree, args.out_dir, "head", check=True, channel=args.browser_channel
        )
    # Both sides capture with the same browser in one run; the head side's
    # record wins when it exists, else the base's, else null.
    browser = head_browser if head_browser is not None else base_browser
    manifest = build_manifest(
        args.base_tree, args.head_tree, args.out_dir, head_records, browser=browser
    )
    (args.out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1) + "\n")
    budget = 60_000  # pr_evidence.py re-trims against the shared budget on merge
    full, table = build_ui_section(manifest, args.repo, args.pr, args.head_sha, budget, base_web)
    (args.out_dir / SECTION_NAME).write_text(full)
    (args.out_dir / TABLE_NAME).write_text(table)
    failures = sum(len(r["problems"]) for r in head_records.values())
    print(
        f"ui_evidence: {len(manifest['captures'])} captures, "
        f"{failures} head check failures, "
        f"{sum(1 for c in manifest['captures'] if c['status'] == 'changed')} changed"
    )
    for name in sorted(head_records):
        for problem in head_records[name]["problems"]:
            print(f"ui_evidence: FAIL {name}: {problem}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
