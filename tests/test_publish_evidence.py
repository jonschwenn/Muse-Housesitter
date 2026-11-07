"""Tests for scripts/publish_evidence.py. No network, no browser."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Share one copy: publish_evidence does "import pr_evidence", which must
    # resolve to the same module object the tests patch (e.g. monkeypatching
    # MAX_COMMENT_BYTES). No other test file registers these names.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ui_evidence = _load("ui_evidence")
pr_evidence = _load("pr_evidence")
publish_evidence = _load("publish_evidence")

REPO = "example-owner/example-repo"
PR = 7
HEAD_SHA = "a" * 40

TEXT = pr_evidence.MARKER + "\n## Before / after evidence\n\n**No output change.**\n"


def _png(color=(10, 20, 30)):
    r, g, b = color
    return ui_evidence.encode_png(4, 4, 2, bytes([r, g, b]) * 16)


def _manifest(*names):
    captures = []
    for name in names:
        captures.append(
            {
                "fixture": "typical",
                "view": "index-phone",
                "status": "added",
                "base": None,
                "head": name,
                "diff": None,
                "head_problems": [],
            }
        )
    return {"version": 1, "captures": captures}


def _artifact(tmp_path, text=TEXT, pngs=("typical__index-phone__head.png",), manifest=None):
    artifact = tmp_path / "artifact"
    (artifact / "evidence.md").parent.mkdir(parents=True)
    (artifact / "evidence.md").write_text(text)
    ui_out = artifact / "ui_out"
    ui_out.mkdir()
    for i, name in enumerate(pngs):
        (ui_out / name).write_bytes(_png((10 + i, 20, 30)))
    if manifest is None and pngs:
        manifest = _manifest(*pngs)
    if manifest is not None:
        (ui_out / "ui_manifest.json").write_text(json.dumps(manifest))
    return artifact


def _render(tmp_path, artifact, **kwargs):
    out = tmp_path / "out"
    rc = publish_evidence.main(
        [
            "render",
            "--artifact-dir", str(artifact),
            "--out-dir", str(out),
            "--repo", kwargs.get("repo", REPO),
            "--pr", str(kwargs.get("pr", PR)),
            "--head-sha", kwargs.get("head_sha", HEAD_SHA),
        ]
    )
    return rc, out


# Text-only path ---------------------------------------------------------------


def test_render_without_ui_dir_publishes_text_only(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "evidence.md").write_text(TEXT)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    assert (out / "comment.md").read_text() == TEXT
    assert list((out / "files").iterdir()) == []


def test_render_without_manifest_publishes_text_only(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    assert "## Web UI" not in (out / "comment.md").read_text()


def test_render_empty_manifest_notes_nothing_to_capture(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest={"version": 1, "captures": []})
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "nothing to show" in body
    # The manifest alone is pushed; with no PNGs the workflow skips the push.
    assert sorted(p.name for p in (out / "files").iterdir()) == ["ui_manifest.json"]


# Valid artifact -----------------------------------------------------------------


def test_render_valid_artifact_builds_ui_section_from_manifest(tmp_path):
    artifact = _artifact(tmp_path)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert body.startswith(pr_evidence.MARKER)
    assert "## Web UI" in body
    assert "typical__index-phone__head.png" in body
    assert ui_evidence.SHA_PLACEHOLDER in body
    files = sorted(p.name for p in (out / "files").iterdir())
    assert files == ["typical__index-phone__head.png", "ui_manifest.json"]


def test_render_adds_failure_notice_after_marker(tmp_path):
    manifest = _manifest("typical__index-phone__head.png")
    manifest["captures"][0]["head_problems"] = ["console.error: boom"]
    artifact = _artifact(tmp_path, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    lines = (out / "comment.md").read_text().split("\n")
    assert lines[0] == pr_evidence.MARKER
    assert lines[1].startswith("**Web UI checks failed:** 1 capture(s)")
    assert "console.error: boom" in "\n".join(lines)


def test_render_ignores_pre_rendered_markdown(tmp_path):
    artifact = _artifact(tmp_path)
    (artifact / "ui_out" / "ui_section.md").write_text("PWNED\n")
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


# Refusals ---------------------------------------------------------------------


def test_render_refuses_text_without_marker(tmp_path):
    artifact = _artifact(tmp_path)
    (artifact / "evidence.md").write_text("no marker here\n")
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_oversized_text(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 10)
    artifact = _artifact(tmp_path)
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_non_png(tmp_path):
    artifact = _artifact(tmp_path)
    (artifact / "ui_out" / "typical__eink__head.png").write_bytes(b"not a png")
    manifest = _manifest(
        "typical__index-phone__head.png", "typical__eink__head.png"
    )
    (artifact / "ui_out" / "ui_manifest.json").write_text(json.dumps(manifest))
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_manifest_traversal(tmp_path):
    manifest = _manifest("../typical__index-phone__head.png")
    artifact = _artifact(tmp_path, manifest=manifest)
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_unlisted_png(tmp_path):
    artifact = _artifact(tmp_path, pngs=("typical__index-phone__head.png",))
    (artifact / "ui_out" / "typical__eink__head.png").write_bytes(_png())
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_python_in_artifact(tmp_path):
    artifact = _artifact(tmp_path)
    (artifact / "ui_out" / "evil.py").write_text("print('x')\n")
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_refuses_symlink(tmp_path):
    artifact = _artifact(tmp_path)
    target = artifact / "ui_out" / "typical__index-phone__head.png"
    (artifact / "ui_out" / "typical__eink__head.png").symlink_to(target)
    manifest = _manifest(
        "typical__index-phone__head.png", "typical__eink__head.png"
    )
    (artifact / "ui_out" / "ui_manifest.json").write_text(json.dumps(manifest))
    rc, _ = _render(tmp_path, artifact)
    assert rc == 1


def test_render_rejects_bad_caller_arguments(tmp_path):
    artifact = _artifact(tmp_path)
    rc, _ = _render(tmp_path, artifact, repo="not a repo!")
    assert rc == 2
    rc, _ = _render(tmp_path, artifact, head_sha="xyz")
    assert rc == 2


# Substitute --------------------------------------------------------------------


def test_substitute_pins_the_sha(tmp_path):
    comment = tmp_path / "comment.md"
    comment.write_text("img " + ui_evidence.SHA_PLACEHOLDER + " end\n")
    rc = publish_evidence.main(["substitute", "--comment", str(comment), "--sha", HEAD_SHA])
    assert rc == 0
    body = comment.read_text()
    assert ui_evidence.SHA_PLACEHOLDER not in body
    assert HEAD_SHA in body


def test_substitute_rejects_a_bad_sha(tmp_path):
    comment = tmp_path / "comment.md"
    comment.write_text("img " + ui_evidence.SHA_PLACEHOLDER + " end\n")
    rc = publish_evidence.main(["substitute", "--comment", str(comment), "--sha", "xyz"])
    assert rc == 2
    # The file is untouched on failure.
    assert ui_evidence.SHA_PLACEHOLDER in comment.read_text()


# Browser provenance (issue #8) --------------------------------------------------
#
# An artifact whose manifest carries the "browser" key still validates (the
# key is additive), and the published comment names the capturing browser.
# A hostile browser value renders the neutral sentence instead.


def test_render_artifact_with_browser_key_names_the_browser(tmp_path):
    manifest = _manifest("typical__index-phone__head.png")
    manifest["browser"] = {"channel": "chrome", "version": "131.0.6778"}
    artifact = _artifact(tmp_path, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    assert "Captured headless with Google Chrome 131.0.6778." in (
        out / "comment.md"
    ).read_text()


def test_render_artifact_with_hostile_browser_is_neutral(tmp_path):
    manifest = _manifest("typical__index-phone__head.png")
    manifest["browser"] = {"channel": "chrome<script>", "version": "1.2.3.4.5"}
    artifact = _artifact(tmp_path, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "The manifest does not record a recognised browser." in body
    assert "Captured headless" not in body
    assert "chrome<script>" not in body
    assert "1.2.3.4.5" not in body
