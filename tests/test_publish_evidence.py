"""Tests for scripts/publish_evidence.py. No network, no browser."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

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
# Shared with the publisher under test so monkeypatched caps apply to it.
evidence_checks = publish_evidence.evidence_checks

REPO = "example-owner/example-repo"
PR = 7
HEAD_SHA = "a" * 40

TEXT = pr_evidence.MARKER + "\n## Before / after evidence\n\n**No output change.**\n"


def _results(scenarios=None, **overrides):
    """A minimal valid structured-results doc (Decision 13, option A)."""
    if scenarios is None:
        scenarios = [
            {
                "id": "version",
                "title": "Installed version",
                "command": ["housesitter", "--version"],
                "exit_before": 0,
                "exit_after": 0,
                "expected_exit": 0,
                "size_before": 10,
                "size_after": 10,
                "changed": False,
                "new_in_pr": False,
                "before": "$ exit 0\n",
                "after": "$ exit 0\n",
            }
        ]
    doc = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "head_sha": HEAD_SHA},
        "scenarios": scenarios,
        "removed": [],
    }
    doc.update(overrides)
    return doc


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


def _artifact(
    tmp_path,
    text=TEXT,
    pngs=("typical__index-phone__head.png",),
    manifest=None,
    results=None,
):
    artifact = tmp_path / "artifact"
    (artifact / "evidence.md").parent.mkdir(parents=True)
    (artifact / "evidence.md").write_text(text)
    (artifact / "results.json").write_text(
        json.dumps(_results() if results is None else results)
    )
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
    argv = [
        "render",
        "--artifact-dir", str(artifact),
        "--out-dir", str(out),
        "--repo", kwargs.get("repo", REPO),
        "--pr", str(kwargs.get("pr", PR)),
        "--head-sha", kwargs.get("head_sha", HEAD_SHA),
    ]
    if kwargs.get("run_id") is not None:
        argv += [
            "--run-id", str(kwargs["run_id"]),
            "--run-attempt", str(kwargs["run_attempt"]),
        ]
    rc = publish_evidence.main(argv)
    return rc, out


# Text-only path ---------------------------------------------------------------


# Rebuilt text (Decision 13, option A) --------------------------------------
#
# The comment is rebuilt from results.json; the PR's markdown (evidence.md)
# is never read, let alone posted.


def test_render_ignores_pr_markdown(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None, text="PWNED: Approved\n")
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert body.startswith(pr_evidence.MARKER)
    assert "PWNED" not in body
    assert "produced by the PR's own CI run" in body


def test_render_without_ui_dir_publishes_rebuilt_text_only(tmp_path):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "evidence.md").write_text(TEXT)
    (artifact / "results.json").write_text(json.dumps(_results()))
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert body.startswith(pr_evidence.MARKER)
    assert "**No output change.**" in body
    assert "## Web UI" not in body
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


def test_render_requires_results_json(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    (artifact / "results.json").unlink()
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()


def test_render_refuses_malformed_results_json(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    (artifact / "results.json").write_text("{nope")
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()


def test_render_refuses_unknown_scenario_id(tmp_path):
    bad = _results()
    bad["scenarios"][0]["id"] = "forged-scenario"
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=bad)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()


def test_render_accepts_new_in_pr_scenario_id(tmp_path):
    doc = _results()
    doc["scenarios"][0]["id"] = "forged-scenario"
    doc["scenarios"][0]["new_in_pr"] = True
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=doc)
    rc, _ = _render(tmp_path, artifact)
    assert rc == 0


def test_render_refuses_stale_head_sha(tmp_path):
    doc = _results()
    doc["metadata"]["head_sha"] = "b" * 40
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=doc)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()


# Fixed refusal notices (Decision 4, option B) --------------------------------
#
# Every refusal writes refusal.json with a reason code from the fixed enum,
# and the `refusal` subcommand turns it into main's fixed notice. One test
# per reason code; no silent no-comment outcome remains.


def _refusal_reason(tmp_path, out):
    return json.loads((out / publish_evidence.REFUSAL_FILENAME).read_text())["reason"]


def test_refusal_missing_when_results_json_absent(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    (artifact / "results.json").unlink()
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "missing"
    assert not (out / "comment.md").exists()
    assert not (out / "files").exists()


def test_refusal_too_large_when_results_json_oversized(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence_checks, "RESULTS_MAX_BYTES", 10)
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "too_large"


def test_refusal_too_large_when_pngs_exceed_total_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_evidence, "MAX_TOTAL_BYTES", 10)
    artifact = _artifact(tmp_path)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "too_large"


def test_refusal_bad_png_for_a_fake_screenshot(tmp_path):
    artifact = _artifact(tmp_path)
    (artifact / "ui_out" / "typical__index-phone__head.png").write_bytes(b"not a png")
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_png"


def test_refusal_bad_png_for_a_bad_crc(tmp_path):
    # Magic bytes alone pass; the structural check must still refuse.
    artifact = _artifact(tmp_path)
    path = artifact / "ui_out" / "typical__index-phone__head.png"
    bad = bytearray(path.read_bytes())
    bad[40] ^= 0xFF
    path.write_bytes(bytes(bad))
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_png"


def test_repo_encoder_output_passes_structure_check(tmp_path):
    # The pipeline's own encoder output must validate: no false refusals
    # on honest screenshots.
    artifact = _artifact(tmp_path)
    out = tmp_path / "out"
    rc = publish_evidence.main(
        [
            "render",
            "--artifact-dir", str(artifact),
            "--out-dir", str(out),
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
        ]
    )
    assert rc == 0
    assert (out / "comment.md").exists()


def test_refusal_bad_schema_for_unknown_scenario_id(tmp_path):
    bad = _results()
    bad["scenarios"][0]["id"] = "forged-scenario"
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=bad)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_schema"


def test_refusal_stale_head_for_mismatched_sha(tmp_path):
    doc = _results()
    doc["metadata"]["head_sha"] = "b" * 40
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=doc)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "stale_head"


def test_refusal_subcommand_prints_fixed_notice(tmp_path):
    (tmp_path / "refusal.json").write_text(json.dumps({"reason": "too_large"}))
    rc = publish_evidence.main(
        ["refusal", "--reason-file", str(tmp_path / "refusal.json")]
    )
    assert rc == 0


def test_refusal_subcommand_rejects_a_bad_reason(tmp_path):
    (tmp_path / "refusal.json").write_text(json.dumps({"reason": "pwned"}))
    rc = publish_evidence.main(
        ["refusal", "--reason-file", str(tmp_path / "refusal.json")]
    )
    assert rc == 2


def test_refusal_subcommand_rejects_a_missing_file(tmp_path):
    rc = publish_evidence.main(
        ["refusal", "--reason-file", str(tmp_path / "no-such-file.json")]
    )
    assert rc == 2


def test_refusal_notice_carries_no_artifact_content(tmp_path, capsys):
    # Even a hostile artifact shapes nothing: the notice is fixed text.
    bad = _results()
    bad["scenarios"][0]["after"] = "PWNED Approved by maintainer"
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=bad)
    bad["scenarios"][0]["id"] = "forged"
    (artifact / "results.json").write_text(json.dumps(bad))
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_schema"
    rc = publish_evidence.main(
        ["refusal", "--reason-file", str(out / publish_evidence.REFUSAL_FILENAME)]
    )
    assert rc == 0
    notice = capsys.readouterr().out
    assert notice.startswith(evidence_checks.REFUSAL_MARKER + "\n")
    assert "`bad_schema`" in notice
    assert "PWNED" not in notice
    assert "Approved by maintainer" not in notice


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


def test_render_refuses_oversized_results_text(tmp_path, monkeypatch):
    # The rebuilt text section, not the PR's markdown, is what must fit.
    doc = _results()
    doc["scenarios"][0]["changed"] = True
    doc["scenarios"][0]["new_in_pr"] = True
    doc["scenarios"][0]["after"] = "x" * 10_000
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=doc)
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 10)
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


# Issue #22 --------------------------------------------------------------------
#
# A manifest without a captures list is an invalid artifact (exit 1), never a
# bad caller argument (exit 2): validate_artifact used to default the missing
# key to [], then load_ui_manifest raised ValueError, which main reported as
# exit 2.


@pytest.mark.parametrize(
    "manifest",
    [{"version": 1}, {"version": 1, "captures": {}}, {"version": 1, "captures": None}],
)
def test_render_manifest_without_captures_list_is_invalid_artifact(tmp_path, manifest):
    artifact = _artifact(tmp_path, pngs=(), manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()
    assert not (out / "files").exists()


def test_render_deeply_nested_manifest_is_invalid_artifact(tmp_path, capsys):
    # Well under the size cap, but deep enough to exhaust json's recursion.
    artifact = _artifact(tmp_path, pngs=())
    (artifact / "ui_out" / "ui_manifest.json").write_text("[" * 50_000 + "]" * 50_000)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err
    assert not (out / "comment.md").exists()


@pytest.mark.parametrize("kind", ["symlink", "broken-symlink", "directory"])
def test_render_manifest_not_a_regular_file_is_invalid_artifact(tmp_path, kind):
    # Not "no UI evidence": a symlink or directory in the manifest's place
    # must refuse the publish, not quietly drop the Web UI section.
    artifact = _artifact(tmp_path)
    manifest = artifact / "ui_out" / "ui_manifest.json"
    manifest.unlink()
    if kind == "symlink":
        manifest.symlink_to("typical__index-phone__head.png")
    elif kind == "broken-symlink":
        manifest.symlink_to("does-not-exist.json")
    else:
        manifest.mkdir()
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert not (out / "comment.md").exists()
    assert not (out / "files").exists()


def test_render_non_utf8_manifest_is_invalid_artifact(tmp_path, capsys):
    artifact = _artifact(tmp_path, pngs=())
    (artifact / "ui_out" / "ui_manifest.json").write_bytes(b'{"captures": ["\xff"]}')
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err
    assert not (out / "comment.md").exists()


def test_render_unreadable_manifest_is_invalid_artifact(tmp_path, monkeypatch, capsys):
    # chmod 000 doesn't stop root, so simulate the read failing.
    artifact = _artifact(tmp_path)
    real_read_text = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "ui_manifest.json":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err
    assert not (out / "comment.md").exists()


@pytest.mark.parametrize("captures", [[42], {"head": "x"}, None])
def test_copy_pushable_files_refuses_bad_capture_shapes(tmp_path, captures):
    # Defence in depth: the copy step re-reads the manifest and must refuse,
    # not crash, even though validation normally stops these first.
    ui_dir = tmp_path / "ui_out"
    ui_dir.mkdir()
    (ui_dir / "ui_manifest.json").write_text(json.dumps({"version": 1, "captures": captures}))
    with pytest.raises(publish_evidence.PublishError):
        publish_evidence.copy_pushable_files(ui_dir, tmp_path / "dest")


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


def test_render_capture_without_head_problems_publishes(tmp_path):
    # A hostile manifest that omits head_problems passes validation; the
    # publisher must still render (fail closed or publish, never crash).
    manifest = _manifest("typical__index-phone__head.png")
    del manifest["captures"][0]["head_problems"]
    artifact = _artifact(tmp_path, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    assert "typical / index-phone" in (out / "comment.md").read_text()


# Removed captures (issue #21) -------------------------------------------------
#
# A removed capture keeps its base PNG on the pr-evidence branch, so the
# published comment shows the before image and the push carries the base PNG.


def test_render_removed_capture_publishes_before_image(tmp_path):
    manifest = {
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
    artifact = _artifact(
        tmp_path, pngs=("typical__index-phone__base.png",), manifest=manifest
    )
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "Removed page: no after image (the head no longer has this page)." in body
    assert "typical__index-phone__base.png" in body
    assert "after: typical / index-phone" not in body
    assert "diff: typical / index-phone" not in body
    files = sorted(p.name for p in (out / "files").iterdir())
    assert files == ["typical__index-phone__base.png", "ui_manifest.json"]


# Publisher/comment agreement (adversarial review) -----------------------------
#
# A hostile capture that names files its status must not show (removed + head,
# removed + diff, added + base, unchanged + names) is dropped from the
# comment, and the push carries exactly the pictured files: no unreferenced
# PNG lands on the pr-evidence branch. The same holds for captures dropped
# for any other reason (e.g. a bad view).


def _capture(fixture, view, status, base, head, diff, **extra):
    capture = {
        "fixture": fixture,
        "view": view,
        "status": status,
        "base": base,
        "head": head,
        "diff": diff,
        "head_problems": [],
    }
    capture.update(extra)
    return capture


def _img_names(body):
    import re

    return sorted(
        src.rsplit("/", 1)[-1] for src in re.findall(r'<img src="([^"]+)"', body)
    )


@pytest.mark.parametrize(
    "capture, pngs, banned",
    [
        (
            _capture(
                "typical",
                "index-phone",
                "removed",
                "typical__index-phone__base.png",
                "typical__index-phone__head.png",
                None,
            ),
            ("typical__index-phone__base.png", "typical__index-phone__head.png"),
            ["typical__index-phone__head.png"],
        ),
        (
            _capture(
                "typical",
                "index-phone",
                "removed",
                "typical__index-phone__base.png",
                None,
                "typical__index-phone__diff.png",
            ),
            ("typical__index-phone__base.png", "typical__index-phone__diff.png"),
            ["typical__index-phone__diff.png"],
        ),
        (
            _capture(
                "typical",
                "index-phone",
                "added",
                "typical__index-phone__base.png",
                "typical__index-phone__head.png",
                None,
            ),
            ("typical__index-phone__base.png", "typical__index-phone__head.png"),
            ["typical__index-phone__base.png"],
        ),
        (
            _capture(
                "typical",
                "index-phone",
                "unchanged",
                "typical__index-phone__base.png",
                "typical__index-phone__head.png",
                None,
            ),
            ("typical__index-phone__base.png", "typical__index-phone__head.png"),
            [
                "typical__index-phone__base.png",
                "typical__index-phone__head.png",
            ],
        ),
    ],
)
def test_render_drops_forbidden_names_and_pushes_only_pictured(
    tmp_path, capture, pngs, banned
):
    manifest = {"version": 1, "captures": [capture]}
    artifact = _artifact(tmp_path, pngs=pngs, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "Omitted 1 UI capture(s) with invalid manifest entries." in body
    assert "<img" not in body
    for name in banned:
        assert name not in body
    files = sorted(p.name for p in (out / "files").iterdir())
    assert files == ["ui_manifest.json"]


@pytest.mark.parametrize(
    "capture",
    [
        _capture(
            "typical",
            "index-phone",
            "removed",
            "typical__index-phone__base.png",
            None,
            None,
            diff_error="diff unavailable: boom",
        ),
        _capture(
            "typical",
            "index-phone",
            "removed",
            "typical__index-phone__base.png",
            None,
            None,
            head_problems=["console.error: boom"],
        ),
        _capture(
            "typical",
            "index-phone",
            "added",
            None,
            "typical__index-phone__head.png",
            None,
            diff_error="diff unavailable: boom",
        ),
    ],
)
def test_render_drops_forbidden_check_fields(tmp_path, capture):
    name = capture["base"] or capture["head"]
    manifest = {"version": 1, "captures": [capture]}
    artifact = _artifact(tmp_path, pngs=(name,), manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "Omitted 1 UI capture(s) with invalid manifest entries." in body
    assert "boom" not in body
    assert "<img" not in body
    assert "Web UI checks failed" not in body
    files = sorted(p.name for p in (out / "files").iterdir())
    assert files == ["ui_manifest.json"]


def test_render_does_not_push_files_from_entries_dropped_for_bad_view(tmp_path):
    capture = _capture(
        "typical",
        "not-a-view",
        "changed",
        "typical__index-phone__base.png",
        "typical__index-phone__head.png",
        None,
    )
    manifest = {"version": 1, "captures": [capture]}
    artifact = _artifact(
        tmp_path,
        pngs=(
            "typical__index-phone__base.png",
            "typical__index-phone__head.png",
        ),
        manifest=manifest,
    )
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "Omitted 1 UI capture(s) with invalid manifest entries." in body
    files = sorted(p.name for p in (out / "files").iterdir())
    assert files == ["ui_manifest.json"]


def test_pushed_files_equal_pictured_files_over_mixed_manifest(tmp_path):
    manifest = {
        "version": 1,
        "captures": [
            _capture(
                "added-fix",
                "index-phone",
                "added",
                None,
                "added-fix__index-phone__head.png",
                None,
            ),
            _capture(
                "removed-fix",
                "index-desktop",
                "removed",
                "removed-fix__index-desktop__base.png",
                None,
                None,
            ),
            _capture(
                "changed-fix",
                "eink",
                "changed",
                "changed-fix__eink__base.png",
                "changed-fix__eink__head.png",
                "changed-fix__eink__diff.png",
            ),
            _capture(
                "nodiff-fix",
                "index-desktop",
                "changed",
                "nodiff-fix__index-desktop__base.png",
                "nodiff-fix__index-desktop__head.png",
                None,
                diff_error="diff unavailable: boom",
            ),
            _capture(
                "same-fix",
                "index-phone",
                "unchanged",
                None,
                None,
                None,
            ),
        ],
    }
    pngs = (
        "added-fix__index-phone__head.png",
        "removed-fix__index-desktop__base.png",
        "changed-fix__eink__base.png",
        "changed-fix__eink__head.png",
        "changed-fix__eink__diff.png",
        "nodiff-fix__index-desktop__base.png",
        "nodiff-fix__index-desktop__head.png",
    )
    artifact = _artifact(tmp_path, pngs=pngs, manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    body = (out / "comment.md").read_text()
    full, _, _ = pr_evidence.render_ui_section(manifest, REPO, PR, HEAD_SHA)
    assert _img_names(body) == _img_names(full)
    assert sorted(_img_names(full)) == sorted(pngs)
    files = sorted(
        p.name for p in (out / "files").iterdir() if p.name != "ui_manifest.json"
    )
    assert files == sorted(_img_names(full))



# Test results in the comment (issue #11) --------------------------------------
#
# The new --test-results-dir / --jobs-json args are optional: without them the
# comment is byte-for-byte what it was. With them, a "**Tests:**" line is
# inserted right after the evidence heading. Bad results data degrades only
# the test line (option A); bad UI artifacts still refuse the whole publish.

def _load_test_results():
    spec = importlib.util.spec_from_file_location(
        "test_results", ROOT / "scripts" / "test_results.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_results"] = module
    spec.loader.exec_module(module)
    return module


_test_results = _load_test_results()


def _junit(version="3.9.25", tests=10, failures=0, errors=0, skipped=0,
           failing=()):
    cases = []
    for i in range(tests):
        name = f"test_case_{i:02d}"
        if name in failing:
            cases.append(
                f'<testcase classname="tests/test_sample.py" name="{name}">'
                "<failure>boom</failure></testcase>"
            )
        else:
            cases.append(
                f'<testcase classname="tests/test_sample.py" name="{name}"/>'
            )
    return (
        f'<?xml version="1.0"?><testsuite name="python-{version}" '
        f'tests="{tests}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}">{"".join(cases)}</testsuite>'
    ).encode()


def _results_tree(tmp_path, versions):
    root = tmp_path / "test-results"
    for short, payload in versions.items():
        sub = root / f"test-results-{short}"
        sub.mkdir(parents=True)
        (sub / "junit.xml").write_bytes(payload)
    return root


def _jobs_doc(*jobs):
    return {"total_count": len(jobs), "jobs": list(jobs)}


def _job(name, conclusion):
    return {"name": name, "conclusion": conclusion}


def _render_with_results(tmp_path, artifact, results_dir=None, jobs=None):
    out = tmp_path / "out"
    argv = [
        "render",
        "--artifact-dir", str(artifact),
        "--out-dir", str(out),
        "--repo", REPO,
        "--pr", str(PR),
        "--head-sha", HEAD_SHA,
    ]
    if results_dir is not None:
        argv += ["--test-results-dir", str(results_dir)]
    jobs_path = tmp_path / "jobs.json"
    if jobs is not None:
        jobs_path.write_text(json.dumps(jobs))
        argv += ["--jobs-json", str(jobs_path)]
    rc = publish_evidence.main(argv)
    return rc, out


def test_render_without_result_args_is_unchanged(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    results = _results_tree(tmp_path, {"3.9": _junit()})
    assert results.exists()
    rc, out = _render(tmp_path, artifact)
    assert rc == 0
    text = (out / "comment.md").read_text()
    assert text.startswith(pr_evidence.MARKER)
    assert "**No output change.**" in text
    assert "**Tests:**" not in text


def test_render_with_results_for_two_versions(tmp_path):
    artifact = _artifact(tmp_path)
    results = _results_tree(
        tmp_path, {"3.9": _junit("3.9.25"), "3.x": _junit("3.14.1")}
    )
    jobs = _jobs_doc(_job("test (3.9)", "success"), _job("test (3.x)", "success"))
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "**Tests:** 10 passed on 3.9.25 and 3.14.1." in body
    lines = body.split("\n")
    heading = lines.index("## Before / after evidence")
    assert lines[heading + 1].startswith("**Tests:**")


def test_render_with_failing_version(tmp_path):
    artifact = _artifact(tmp_path)
    results = _results_tree(
        tmp_path,
        {"3.x": _junit("3.14.1", tests=3, failures=1,
                       failing=("test_case_00",))},
    )
    jobs = _jobs_doc(_job("test (3.x)", "failure"))
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "**Tests:** **failed** on 3.14.1" in body
    assert "`tests/test_sample.py::test_case_00`" in body


def test_render_with_missing_results_dir_degrades_line(tmp_path):
    artifact = _artifact(tmp_path)
    jobs = _jobs_doc(_job("test (3.9)", "success"))
    rc, out = _render_with_results(
        tmp_path, artifact, tmp_path / "no-such-dir", jobs
    )
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "no valid results file" in body


def test_render_with_missing_jobs_json_is_unavailable(tmp_path):
    artifact = _artifact(tmp_path)
    results = _results_tree(tmp_path, {"3.9": _junit()})
    out = tmp_path / "out"
    rc = publish_evidence.main(
        [
            "render",
            "--artifact-dir", str(artifact),
            "--out-dir", str(out),
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
            "--test-results-dir", str(results),
            "--jobs-json", str(tmp_path / "no-jobs.json"),
        ]
    )
    assert rc == 0
    assert "**Tests:** results unavailable." in (out / "comment.md").read_text()


def test_render_with_one_bad_results_entry_degrades_only_it(tmp_path):
    artifact = _artifact(tmp_path)
    root = tmp_path / "test-results"
    good = root / "test-results-3.9"
    good.mkdir(parents=True)
    (good / "junit.xml").write_bytes(_junit("3.9.25"))
    bad = root / "test-results-3.x"
    bad.mkdir(parents=True)
    (bad / "junit.xml").write_bytes(_junit("3.14.1"))
    (bad / "extra.txt").write_text("hostile extra entry\n")
    jobs = _jobs_doc(_job("test (3.9)", "success"), _job("test (3.x)", "success"))
    rc, out = _render_with_results(tmp_path, artifact, root, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "10 passed on 3.9.25" in body
    assert "no valid results file" in body


def test_render_with_symlinked_junit_degrades_only_it(tmp_path):
    artifact = _artifact(tmp_path)
    root = tmp_path / "test-results"
    good = root / "test-results-3.9"
    good.mkdir(parents=True)
    (good / "junit.xml").write_bytes(_junit("3.9.25"))
    bad = root / "test-results-3.x"
    bad.mkdir(parents=True)
    (bad / "real.xml").write_bytes(_junit("3.14.1"))
    (bad / "junit.xml").symlink_to(bad / "real.xml")
    jobs = _jobs_doc(_job("test (3.9)", "success"), _job("test (3.x)", "success"))
    rc, out = _render_with_results(tmp_path, artifact, root, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "10 passed on 3.9.25" in body
    assert "no valid results file" in body


def test_render_with_bad_artifact_dir_name_degrades_line(tmp_path):
    artifact = _artifact(tmp_path)
    root = tmp_path / "test-results"
    evil = root / "test-results-evil"
    evil.mkdir(parents=True)
    (evil / "junit.xml").write_bytes(_junit("3.9.25"))
    jobs = _jobs_doc(_job("test (3.9)", "success"))
    rc, out = _render_with_results(tmp_path, artifact, root, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert "no valid results file" in body


def test_render_with_too_many_result_dirs_degrades_line(tmp_path):
    artifact = _artifact(tmp_path)
    root = _results_tree(
        tmp_path,
        {
            "3.9": _junit("3.9.25"),
            "3.10": _junit("3.10.12"),
            "3.11": _junit("3.11.9"),
            "3.12": _junit("3.12.4"),
            "3.13": _junit("3.13.0"),
        },
    )
    jobs = _jobs_doc(_job("test (3.9)", "success"))
    rc, out = _render_with_results(tmp_path, artifact, root, jobs)
    assert rc == 0
    assert "no valid results file" in (out / "comment.md").read_text()


@pytest.mark.parametrize(
    "jobs",
    [
        {"jobs": [{"name": "test (3.9)"}]},
        {"jobs": [{"name": "test (3.9)", "conclusion": "exploded"}]},
        {"jobs": "not-a-list"},
        {"no-jobs": []},
        ["a-list-not-a-dict"],
    ],
)
def test_render_with_bad_jobs_json_is_unavailable(tmp_path, jobs):
    artifact = _artifact(tmp_path)
    results = _results_tree(tmp_path, {"3.9": _junit()})
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    assert "**Tests:** results unavailable." in (out / "comment.md").read_text()


def test_render_with_malformed_jobs_json_is_unavailable(tmp_path):
    artifact = _artifact(tmp_path)
    results = _results_tree(tmp_path, {"3.9": _junit()})
    out = tmp_path / "out"
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text("{not json")
    rc = publish_evidence.main(
        [
            "render",
            "--artifact-dir", str(artifact),
            "--out-dir", str(out),
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
            "--test-results-dir", str(results),
            "--jobs-json", str(jobs_path),
        ]
    )
    assert rc == 0
    assert "**Tests:** results unavailable." in (out / "comment.md").read_text()


def test_render_with_results_still_checks_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 10)
    artifact = _artifact(tmp_path)
    results = _results_tree(tmp_path, {"3.9": _junit()})
    jobs = _jobs_doc(_job("test (3.9)", "success"))
    rc, _ = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 1


def test_render_with_hostile_results_never_echoed(tmp_path):
    artifact = _artifact(tmp_path)
    root = tmp_path / "test-results"
    bad = root / "test-results-3.9"
    bad.mkdir(parents=True)
    # Assembled at runtime so the literal never appears in the repo.
    payload = (
        b'<?xml version="1.0"?><testsuite name="python-3.9.25" '
        b'tests="1" failures="1" errors="0" skipped="0">'
        b'<testcase classname="tests/test_a.py" name="test_one">'
        b"<failure>hunter2" + b"hunter2 twelve</failure></testcase></testsuite>"
    )
    (bad / "junit.xml").write_bytes(b"<!DOCTYPE x>" + payload)
    jobs = _jobs_doc(_job("test (3.9)", "failure"))
    rc, out = _render_with_results(tmp_path, artifact, root, jobs)
    assert rc == 0
    text = (out / "comment.md").read_text()
    assert "hunter2" not in text


# Workflow wiring (issue #11) ----------------------------------------------------


def test_publisher_fetches_test_results_and_jobs():
    workflow = (ROOT / ".github" / "workflows" / "evidence-publish.yml").read_text()
    assert "test-results-*" in workflow
    assert "--test-results-dir" in workflow
    assert "--jobs-json" in workflow
    assert "attempts" in workflow
    assert "run_attempt" in workflow
    assert "jobs.json" in workflow


def test_ci_produces_test_results():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "junit.xml" in workflow
    assert "test-results-" in workflow
    assert "test_results.py summary" in workflow
    assert "tests/test_test_results.py" in workflow
    assert "junit_suite_name" in workflow


# Regression tests for the adversarial review ----------------------------------
#
# A near-cap evidence.md plus a Tests line must still publish (rc 0) within
# the comment budget: the Tests line shrinks (names dropped, counts kept)
# and then falls back to a fixed short line, instead of the whole publish
# either failing or posting an over-budget comment. Only evidence that is
# already over budget on its own still refuses (exit 1).


def _padded_results_text(size):
    """A results doc whose rebuilt comment lands just under `size` bytes.

    The comment is rebuilt from results.json now, so near-cap tests pad the
    structured results instead of the ignored evidence.md. Padding uses new
    scenarios with tiny outputs; the renderer keeps whole sections while
    they fit, so the comment grows monotonically here. The last scenario
    is popped back off when the target is overshot, so the comment never
    exceeds `size` (one scenario is a few hundred bytes).
    """
    doc = {
        "version": 1,
        "metadata": {"generator": "scripts/pr_evidence.py", "head_sha": HEAD_SHA},
        "scenarios": [],
        "removed": [],
    }
    index = 0
    while index < 250:
        doc["scenarios"].append(
            {
                "id": f"pad-{index:04d}",
                "title": "",
                "command": ["housesitter", "--version"],
                "exit_before": 0,
                "exit_after": 0,
                "expected_exit": 0,
                "size_before": 10,
                "size_after": 10,
                "changed": True,
                "new_in_pr": True,
                "before": "$ exit 0\n",
                "after": "$ exit 0\n",
            }
        )
        body = pr_evidence.render_text_from_results(doc, HEAD_SHA)
        if len(body.encode()) >= size:
            doc["scenarios"].pop()
            break
        index += 1
    return doc


def _exact_evidence_text(size):
    """A fixed text body of exactly `size` bytes for fit_test_line unit tests.

    Byte-exact padding is not possible through the self-trimming renderer,
    so the shrink ladder (full line, brief line, fallback, dropped) is
    pinned here instead of through rendered artifacts.
    """
    assert size >= len(pr_evidence.MARKER) + 30
    return pr_evidence.MARKER + "\n## Before / after evidence\n\n" + "x" * (
        size - len(pr_evidence.MARKER) - len("\n## Before / after evidence\n\n")
    )


def _text_only_artifact(tmp_path, size):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "evidence.md").write_text(TEXT)  # ignored by the new publisher
    (artifact / "results.json").write_text(json.dumps(_padded_results_text(size)))
    return artifact


def _max_names_xml(version="3.14.1", count=20):
    cases = []
    for i in range(count):
        name = f"test_case_{i:02d}_" + "n" * 165
        assert len(f"tests/test_sample.py::{name}") == 200
        cases.append(
            f'<testcase classname="tests/test_sample.py" name="{name}">'
            "<failure>boom</failure></testcase>"
        )
    return (
        f'<?xml version="1.0"?><testsuite name="python-{version}" '
        f'tests="{count}" failures="{count}" errors="0" skipped="0">'
        + "".join(cases) + "</testsuite>"
    ).encode()


def test_render_text_only_near_cap_tests_line_stays_in_budget(tmp_path, monkeypatch):
    # A near-cap rebuilt text plus a Tests line must still publish within
    # the comment budget, keeping the failure counts. Whether the failing
    # names survive depends on the exact room, so the shrink ladder itself
    # is pinned by the fit_test_line unit tests below.
    cap = 3000
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", cap)
    artifact = _text_only_artifact(tmp_path, cap - 500)
    results = _results_tree(
        tmp_path,
        {"3.14": _junit("3.14.1", tests=3, failures=2,
                        failing=("test_case_00", "test_case_01"))},
    )
    jobs = _jobs_doc(_job("test (3.14)", "failure"))
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert len(body.encode()) <= cap
    assert "2 failing" in body


def test_render_text_only_near_cap_many_long_names_stays_in_budget(tmp_path, monkeypatch):
    cap = 3000
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", cap)
    artifact = _text_only_artifact(tmp_path, cap - 500)
    results = _results_tree(tmp_path, {"3.14": _max_names_xml()})
    jobs = _jobs_doc(_job("test (3.14)", "failure"))
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert len(body.encode()) <= cap
    assert "20 failing" in body


def test_fit_test_line_shrink_ladder(monkeypatch):
    # Byte-exact evidence sizes pin the D21 shrink order: the full Tests
    # line first, then the brief line without names, then the fixed short
    # line, then nothing. Counts and verdicts survive every step that
    # keeps a line. Pure text: the renderers' real output is covered by
    # the integration tests above.
    cap = 3000
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", cap)
    full = "**Tests:** **failed** on 3.9, 3.10, 3.11 (12 failing: `n1`, `n2`, `n3`)."
    brief = "**Tests:** **failed** on 3.9, 3.10, 3.11 (12 failing)."
    fallback = publish_evidence.TEST_LINE_FALLBACK
    assert len(full) > len(brief) > len(fallback)

    def room_for(line, spare):
        # Evidence size leaving `spare` bytes of room after inserting `line`.
        return cap - len(line.encode()) - 1 - spare

    # Room for the full line: it is kept.
    body = publish_evidence.fit_test_line(_exact_evidence_text(room_for(full, 5)), full, brief)
    assert full in body
    # Room for the brief line only: names are dropped, counts stay.
    body = publish_evidence.fit_test_line(_exact_evidence_text(room_for(brief, 5)), full, brief)
    assert brief in body and "n1" not in body
    # Room only for the fixed short line.
    body = publish_evidence.fit_test_line(
        _exact_evidence_text(room_for(fallback, 2)), full, brief
    )
    assert fallback in body and "12 failing" not in body
    # No room for any line: it is left out, and the publish stays in budget.
    body = publish_evidence.fit_test_line(_exact_evidence_text(cap - 10), full, brief)
    assert "**Tests:**" not in body
    assert len(body.encode()) <= cap


def test_render_ui_path_near_cap_tests_line_stays_in_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 3000)
    artifact = _artifact(tmp_path)
    results = _results_tree(tmp_path, {"3.9": _max_names_xml("3.9.25")})
    jobs = _jobs_doc(_job("test (3.9)", "failure"))
    rc, out = _render_with_results(tmp_path, artifact, results, jobs)
    assert rc == 0
    body = (out / "comment.md").read_text()
    assert len(body.encode()) <= 3000
    assert "## Web UI" in body
    assert "(20 failing)" in body
    assert "n" * 165 not in body


@pytest.mark.parametrize("order", [["success", "failure"], ["failure", "success"]])
def test_validate_jobs_duplicate_version_keeps_worst(tmp_path, order):
    # A PR controls its own ci.yml and could add a same-named job; the worst
    # conclusion must win so a failure can never be masked, either order.
    docs = [_jobs_doc(*[_job("test (3.9)", conclusion) for conclusion in order])]
    assert publish_evidence.validate_jobs(docs) == [
        {"version": "3.9", "conclusion": "failure"}
    ]


def test_test_results_module_is_stdlib_only():
    import ast

    source = (ROOT / "scripts" / "test_results.py").read_text()
    tree = ast.parse(source)
    stdlib = {
        "argparse", "re", "xml.etree.ElementTree", "pathlib",
        "xml", "sys", "__future__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in {
                    s.split(".")[0] for s in stdlib
                }, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in {
                s.split(".")[0] for s in stdlib
            }, node.module


# Re-review fixes (#11) -----------------------------------------------------------


def test_render_ui_path_tests_line_never_tips_the_publish_into_refusal(tmp_path, monkeypatch):
    # Find the largest evidence that publishes with its Web UI section and no
    # test data; adding test data must never turn that into a refusal.
    monkeypatch.setattr(pr_evidence, "MAX_COMMENT_BYTES", 3000)
    jobs = _jobs_doc(_job("test (3.9)", "failure"))
    for size in range(2990, 1000, -5):
        base = tmp_path / f"base{size}"
        base.mkdir()
        artifact = _artifact(base, results=_padded_results_text(size))
        rc, _ = _render(base, artifact)
        if rc == 0:
            break
    else:
        pytest.fail("no evidence size publishes without test data")
    rc, out = _render_with_results(base, artifact, None, jobs)
    assert rc == 0
    assert len((out / "comment.md").read_text().encode()) <= 3000


def test_render_deeply_nested_jobs_json_degrades_only_the_line(tmp_path):
    artifact = _text_only_artifact(tmp_path, 1000)
    out = tmp_path / "out"
    jobs_path = tmp_path / "deep.json"
    jobs_path.write_text("[" * 20_000)
    rc = publish_evidence.main([
        "render", "--artifact-dir", str(artifact), "--out-dir", str(out),
        "--repo", REPO, "--pr", str(PR), "--head-sha", HEAD_SHA,
        "--jobs-json", str(jobs_path),
    ])
    assert rc == 0
    assert "**Tests:** results unavailable." in (out / "comment.md").read_text()


# Final-review fixes (#11) ---------------------------------------------------------


def test_oversized_junit_is_refused_without_reading_it(tmp_path, monkeypatch):
    # Size is checked before the file is read into memory.
    results = _results_tree(tmp_path, {"3.9": _junit("3.9.25")})
    junit = results / "test-results-3.9" / "junit.xml"
    junit.write_bytes(b" " * 2048)
    monkeypatch.setattr(publish_evidence.test_results, "MAX_JUNIT_BYTES", 1024)
    real_read_bytes = Path.read_bytes

    def read_bytes(self):
        assert self.name != "junit.xml", "oversized junit.xml was read"
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert publish_evidence.load_test_results(results) == {"3.9": "invalid"}


def test_load_json_docs_merges_concatenated_pages(tmp_path):
    path = tmp_path / "jobs.json"
    page1 = _jobs_doc(_job("test (3.9)", "success"))
    page2 = _jobs_doc(_job("test (3.9)", "failure"))
    page1["total_count"] = page2["total_count"] = 2
    path.write_text(json.dumps(page1) + "\n" + json.dumps(page2))
    docs = publish_evidence.load_json_docs(path, publish_evidence.JOBS_MAX_BYTES)
    assert docs == [page1, page2]
    # The failure on page 2 can't be masked by page 1's success.
    assert publish_evidence.validate_jobs(docs) == [
        {"version": "3.9", "conclusion": "failure"}
    ]


def test_validate_jobs_refuses_a_truncated_listing():
    # total_count says there are more jobs than were fetched: a failing test
    # job could be on a missing page, so the data is unusable.
    page = _jobs_doc(_job("test (3.9)", "success"))
    page["total_count"] = 5
    assert publish_evidence.validate_jobs([page]) is None


def test_insert_test_line_without_heading_goes_after_the_marker():
    text = pr_evidence.MARKER + "\nno heading here\n"
    out = publish_evidence.insert_test_line(text, "**Tests:** x")
    assert out.split("\n")[:3] == [pr_evidence.MARKER, "**Tests:** x", "no heading here"]


# Review round 1, finding 3: no malformed artifact ends silently. ------------
#
# Missing fields, hostile nesting, lone surrogates and unexpected manifest
# types used to escape render with a traceback (KeyError, RecursionError,
# UnicodeEncodeError-as-caller-error, TypeError) and no refusal.json, so no
# notice. Every case below must exit non-zero with a refusal file.


def test_refusal_bad_schema_when_before_is_missing(tmp_path):
    doc = _results()
    del doc["scenarios"][0]["before"]
    artifact = _artifact(tmp_path, pngs=(), manifest=None, results=doc)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_schema"


def test_refusal_bad_schema_for_deeply_nested_results(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    (artifact / "results.json").write_text("[" * 2000 + "]" * 2000)
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_schema"


def test_refusal_bad_schema_for_a_lone_surrogate(tmp_path):
    doc = _results()
    doc["scenarios"][0]["after"] = "ok\ud800tail"
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    (artifact / "results.json").write_text(json.dumps(doc))
    rc, out = _render(tmp_path, artifact)
    assert rc == 1
    assert _refusal_reason(tmp_path, out) == "bad_schema"


def test_refusal_render_error_for_an_unexpected_manifest_type(tmp_path):
    base = "typical__index-phone__base.png"
    head = "typical__index-phone__head.png"
    manifest = {
        "version": 1,
        "captures": [
            {
                "fixture": "typical",
                "view": [],
                "status": "changed",
                "base": base,
                "head": head,
                "diff": None,
                "head_problems": [],
            }
        ],
    }
    artifact = _artifact(tmp_path, pngs=(base, head), manifest=manifest)
    rc, out = _render(tmp_path, artifact)
    assert rc != 0
    assert _refusal_reason(tmp_path, out) == "render_error"


# Review round 1, finding 4: the comment job never ends silently. ------------
#
# A failed push skipped the comment job, and a build that died before
# uploading left its download failing: both ended with no notice. The
# decision of what to post lives here in Python (unit-tested); the workflow
# only carries it out, falling back to a fixed publish_failed notice built
# from main's text whenever the rendered artifact is missing or the push
# failed. A stale head still writes nothing.


@pytest.mark.parametrize(
    "build,push,refusal,comment,action",
    [
        ("success", "success", False, True, "comment"),
        ("failure", "skipped", True, False, "refusal-notice"),
        ("success", "failure", False, True, "publish-failed"),
        ("success", "failure", False, False, "publish-failed"),
        ("success", "success", False, False, "publish-failed"),
        ("failure", "skipped", False, False, "publish-failed"),
        ("failure", "skipped", False, True, "publish-failed"),
        ("cancelled", "skipped", False, False, "nothing"),
        ("success", "cancelled", False, True, "nothing"),
    ],
)
def test_decide_comment_action(build, push, refusal, comment, action):
    assert (
        publish_evidence.decide_comment_action(build, push, refusal, comment)
        == action
    )


def test_decide_comment_subcommand_prints_only_the_action(capsys):
    rc = publish_evidence.main(
        [
            "decide-comment",
            "--build-result", "success",
            "--push-result", "success",
            "--has-comment",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "comment\n"


# Review round 1, finding 6: re-check the head at the last moment. -----------
#
# The head used to be checked once, then the script fetched, prepared and
# looked up comments: a head that moved mid-step still pushed and posted.
# check-head asks the API for the current head through main's Python (faked
# below), and the workflow calls it immediately before every push and
# immediately before posting, after the comment lookup.


class _FakeCompleted:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_gh(monkeypatch, stdout=HEAD_SHA, returncode=0, calls=None):
    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append(argv)
        return _FakeCompleted(returncode, stdout)

    from types import SimpleNamespace

    monkeypatch.setattr(publish_evidence, "subprocess", SimpleNamespace(run=fake_run))


def test_check_head_passes_when_fresh(tmp_path, monkeypatch, capsys):
    calls = []
    _fake_gh(monkeypatch, calls=calls)
    rc = publish_evidence.main(
        ["check-head", "--repo", REPO, "--pr", str(PR), "--expected", HEAD_SHA]
    )
    assert rc == 0
    assert calls == [["gh", "api", f"repos/{REPO}/pulls/{PR}", "--jq", ".head.sha"]]
    assert capsys.readouterr().out == ""


def test_check_head_stops_when_the_head_moved(monkeypatch, capsys):
    _fake_gh(monkeypatch, stdout="b" * 40)
    rc = publish_evidence.main(
        ["check-head", "--repo", REPO, "--pr", str(PR), "--expected", HEAD_SHA]
    )
    assert rc == 1
    assert "stopping without writing" in capsys.readouterr().err


def test_check_head_errors_when_the_api_fails(monkeypatch):
    _fake_gh(monkeypatch, returncode=1)
    rc = publish_evidence.main(
        ["check-head", "--repo", REPO, "--pr", str(PR), "--expected", HEAD_SHA]
    )
    assert rc == 2


def test_check_head_errors_on_a_bad_api_answer(monkeypatch):
    _fake_gh(monkeypatch, stdout="not-a-sha")
    rc = publish_evidence.main(
        ["check-head", "--repo", REPO, "--pr", str(PR), "--expected", HEAD_SHA]
    )
    assert rc == 2


def test_check_head_rejects_bad_arguments():
    rc = publish_evidence.main(
        ["check-head", "--repo", "not a repo", "--pr", str(PR), "--expected", HEAD_SHA]
    )
    assert rc == 2


# Review round 2, finding 1: a missing rendered directory still posts. --------
#
# When the build fails before uploading validated-evidence, rendered/ does
# not exist; the comment job must still post the fixed publish_failed
# notice instead of dying on the missing directory. This runs the real
# comment-step shell from the workflow with a fake gh on PATH.


def _comment_step_shell():
    """Extract the comment step's `run:` block from evidence-publish.yml."""
    text = (
        ROOT / ".github" / "workflows" / "evidence-publish.yml"
    ).read_text(encoding="utf-8")
    lines = text.splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if line.strip() == "- name: Post or update the evidence comment"
    )
    marker = next(
        i for i in range(start, len(lines)) if lines[i].strip() == "run: |"
    )
    body = []
    for line in lines[marker + 1 :]:
        if line.strip() == "" or line.startswith(" " * 10):
            body.append(line[10:] if line.startswith(" " * 10) else line)
        else:
            break
    assert body, "comment step has no run block"
    return "\n".join(body) + "\n"


_FAKE_GH = """\
#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
state = pathlib.Path(os.environ["FAKE_STATE"])
if "-X" in args:
    i = args.index("-X")
    entry = {"method": args[i + 1], "argv": args}
    for flag in args:
        if flag.startswith("body=@"):
            entry["body"] = pathlib.Path(flag[len("body=@"):]).read_text()
    with (state / "calls.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\\n")
    print("")
elif any("/pulls/" in a for a in args):
    print(os.environ.get("FAKE_HEAD") or os.environ["HEAD_SHA"])
elif any("/comments" in a for a in args):
    listing = state / "listing.json"
    print(listing.read_text() if listing.exists() else "[]")
else:
    raise SystemExit(f"unexpected gh call: {args}")
"""


def _run_comment_step(
    tmp_path,
    monkeypatch,
    build_result,
    push_result,
    files=None,
    listing=None,
    evidence_sha="",
    run_id="100",
    run_attempt="1",
    fake_head=None,
):
    """Run the real comment-step shell with a fake gh; return (proc, calls).

    ``files`` pre-creates run-dir files (e.g. rendered/comment.md);
    ``listing`` is the fake issue-comments API answer; every gh write lands
    in the returned calls list as {"method", "argv", "body"?}.
    """
    import os
    import subprocess

    run = tmp_path / "run"
    run.mkdir()
    (run / "scripts").symlink_to(ROOT / "scripts", target_is_directory=True)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(_FAKE_GH)
    gh.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    if listing is not None:
        (state / "listing.json").write_text(json.dumps(listing))
    for name, content in (files or {}).items():
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    step = tmp_path / "comment-step.sh"
    step.write_text(_comment_step_shell())
    env = {
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "GH_TOKEN": "fake",
        "REPO": REPO,
        "PR": str(PR),
        "HEAD_SHA": HEAD_SHA,
        "EVIDENCE_SHA": evidence_sha,
        "BUILD_RESULT": build_result,
        "PUSH_RESULT": push_result,
        "RUN_ID": run_id,
        "RUN_ATTEMPT": run_attempt,
        "FAKE_STATE": str(state),
    }
    if fake_head is not None:
        env["FAKE_HEAD"] = fake_head
    proc = subprocess.run(
        ["bash", str(step)], cwd=run, env=env,
        capture_output=True, text=True, timeout=120,
    )
    calls = []
    calls_path = state / "calls.jsonl"
    if calls_path.exists():
        calls = [
            json.loads(line) for line in calls_path.read_text().splitlines()
        ]
    return proc, calls


def test_comment_step_posts_notice_with_no_rendered_dir(tmp_path, monkeypatch):
    # Build failed before uploading: no rendered/ directory exists, the
    # download found nothing, and the push was skipped. The fixed
    # publish_failed notice must still reach the PR.
    proc, calls = _run_comment_step(tmp_path, monkeypatch, "failure", "skipped")
    assert proc.returncode == 0, proc.stderr
    posts = [c for c in calls if c["method"] == "POST"]
    assert len(posts) == 1, f"expected one POST; stderr: {proc.stderr}"
    body = posts[0]["body"]
    assert body.startswith("<!-- housesitter-evidence head=")
    assert "outcome=notice" in body.split("\n", 1)[0]
    assert "`publish_failed`" in body


# Review round 2, finding 2: monotonic comment writes. -------------------------
#
# Overlapping publisher runs used to both POST (duplicates), or let an older
# attempt's notice replace a newer run's evidence, or PATCH a stale head's
# body over the current head's. The comment job is now serialised per PR and
# every body carries a hidden marker (head SHA, run id, attempt, outcome);
# the write follows monotonic rules over the bot's own marker comments.

OTHER_HEAD = "b" * 40
BOT = "github-actions[bot]"


def _marker(head=HEAD_SHA, run=100, attempt=1, outcome="evidence"):
    return publish_evidence.build_marker(head, run, attempt, outcome)


def _bot_comment(cid, head=HEAD_SHA, run=100, attempt=1, outcome="evidence",
                 login=BOT):
    return {
        "id": cid,
        "user": {"login": login},
        "body": _marker(head, run, attempt, outcome) + "\n## Before / after evidence\n",
    }


def _triples(comments):
    return [
        (c["id"], c["user"]["login"], c["body"]) for c in comments
    ]


def test_build_marker_rejects_untrusted_shapes():
    good = _marker()
    assert good == (
        f"<!-- housesitter-evidence head={HEAD_SHA} "
        "run=100 attempt=1 outcome=evidence -->"
    )
    for bad in [
        ("x" * 40, 100, 1, "evidence"),
        (HEAD_SHA, 0, 1, "evidence"),
        (HEAD_SHA, -3, 1, "evidence"),
        (HEAD_SHA, True, 1, "evidence"),
        (HEAD_SHA, 100, "1", "evidence"),
        (HEAD_SHA, 100, 1, "refused"),
    ]:
        try:
            publish_evidence.build_marker(*bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"marker accepted {bad!r}")


def test_parse_marker_round_trip_and_rejections():
    assert publish_evidence.parse_marker(_marker()) == {
        "head_sha": HEAD_SHA,
        "run_id": 100,
        "attempt": 1,
        "outcome": "evidence",
    }
    assert publish_evidence.parse_marker(pr_evidence.MARKER + "\nbody") == {
        "legacy": True
    }
    assert publish_evidence.parse_marker("hello") is None
    assert publish_evidence.parse_marker(_marker() + " trailing") is None
    assert publish_evidence.parse_marker(None) is None


def test_stamp_marker_upgrades_the_bare_marker():
    body = pr_evidence.MARKER + "\n## Before / after evidence\n"
    stamped = publish_evidence.stamp_marker(body, HEAD_SHA, 7, 2, "notice")
    assert stamped.split("\n", 1)[0] == _marker(HEAD_SHA, 7, 2, "notice")
    assert stamped.endswith("## Before / after evidence\n")
    try:
        publish_evidence.stamp_marker("no marker here", HEAD_SHA, 7, 2, "notice")
    except ValueError:
        pass
    else:
        raise AssertionError("stamp accepted a body without the marker")


def test_second_same_head_run_patches_instead_of_posting():
    # Interleaving (a): two same-head runs; the later one sees the earlier
    # run's comment and patches it instead of posting a duplicate.
    existing = _triples([_bot_comment(11, run=100)])
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 101, 1, "evidence"
    ) == ("patch", 11, [])


def test_older_same_head_attempt_never_writes():
    existing = _triples([_bot_comment(11, run=101)])
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 100, 1, "evidence"
    ) == ("skip", None, [])


def test_stale_notice_never_replaces_same_head_evidence():
    # Interleaving (b): attempt B published good evidence for head H, then
    # the older attempt A finishes with a failed push: A's publish_failed
    # notice must not replace B's evidence.
    existing = _triples([_bot_comment(11, run=101, outcome="evidence")])
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 100, 1, "notice"
    ) == ("skip", None, [])
    # Even a newer notice never replaces same-head evidence.
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 102, 1, "notice"
    ) == ("skip", None, [])


def test_stale_body_never_reaches_decide_write_for_a_moved_head():
    # Interleaving (c): A read head H1, the head moved, B published H2, and
    # A's stale body arrives last. A's freshness re-check (expected H1 vs
    # current H2) stops it before any write, so the H2 comment survives;
    # decide_write only runs once freshness has established the new head as
    # current, and then a different-head marker is stale by definition.
    existing = _triples([_bot_comment(11, head=OTHER_HEAD, run=101)])
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 100, 1, "evidence"
    ) == ("patch", 11, [])


def test_fresh_head_replaces_a_stale_head_comment():
    existing = _triples([_bot_comment(11, head=OTHER_HEAD, run=90)])
    assert publish_evidence.decide_write(
        existing, HEAD_SHA, 100, 1, "evidence"
    ) == ("patch", 11, [])


def test_forged_markers_in_other_comments_are_ignored():
    forged = {
        "id": 9,
        "user": {"login": "mallory"},
        "body": _marker(run=999) + "\nforged",
    }
    assert publish_evidence.decide_write(
        _triples([forged]), HEAD_SHA, 100, 1, "evidence"
    ) == ("post", None, [])


def test_bot_comment_without_a_marker_is_ignored():
    plain = {"id": 8, "user": {"login": BOT}, "body": "hello"}
    assert publish_evidence.decide_write(
        _triples([plain]), HEAD_SHA, 100, 1, "evidence"
    ) == ("post", None, [])


def test_legacy_bare_marker_is_replaced():
    legacy = {
        "id": 7,
        "user": {"login": BOT},
        "body": pr_evidence.MARKER + "\nold evidence\n",
    }
    assert publish_evidence.decide_write(
        _triples([legacy]), HEAD_SHA, 100, 1, "evidence"
    ) == ("patch", 7, [])


def test_multiple_markers_keep_one_and_delete_the_rest():
    existing = _triples(
        [
            _bot_comment(11, run=90),
            _bot_comment(12, run=95),
            _bot_comment(13, head=OTHER_HEAD, run=200),
        ]
    )
    action, keep, delete = publish_evidence.decide_write(
        existing, HEAD_SHA, 100, 1, "evidence"
    )
    assert action == "patch"
    assert keep == 12
    assert delete == [11, 13]


def test_no_markers_posts():
    assert publish_evidence.decide_write([], HEAD_SHA, 100, 1, "evidence") == (
        "post",
        None,
        [],
    )


def _listing_json(*comments):
    return json.dumps(list(comments))


def test_decide_write_subcommand_prints_patch_plan(monkeypatch, capsys):
    comments = [_bot_comment(5, run=90)]

    def fake_run(argv, **kwargs):
        assert argv[:2] == ["gh", "api"]
        return _FakeCompleted(0, _listing_json(*comments))

    from types import SimpleNamespace

    monkeypatch.setattr(
        publish_evidence, "subprocess", SimpleNamespace(run=fake_run)
    )
    rc = publish_evidence.main(
        [
            "decide-write",
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
            "--run-id", "100",
            "--run-attempt", "1",
            "--outcome", "evidence",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "PATCH 5\n"


def test_decide_write_subcommand_prints_skip_for_stale(monkeypatch, capsys):
    comments = [_bot_comment(5, run=200, outcome="evidence")]

    def fake_run(argv, **kwargs):
        return _FakeCompleted(0, _listing_json(*comments))

    from types import SimpleNamespace

    monkeypatch.setattr(
        publish_evidence, "subprocess", SimpleNamespace(run=fake_run)
    )
    rc = publish_evidence.main(
        [
            "decide-write",
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
            "--run-id", "100",
            "--run-attempt", "1",
            "--outcome", "notice",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out == "SKIP\n"


def test_decide_write_subcommand_fails_loud_when_the_api_fails(monkeypatch):
    _fake_gh(monkeypatch, returncode=1)
    rc = publish_evidence.main(
        [
            "decide-write",
            "--repo", REPO,
            "--pr", str(PR),
            "--head-sha", HEAD_SHA,
            "--run-id", "100",
            "--run-attempt", "1",
            "--outcome", "evidence",
        ]
    )
    assert rc == 2


def test_render_stamps_the_evidence_marker(tmp_path):
    artifact = _artifact(tmp_path, pngs=(), manifest=None)
    rc, out = _render(tmp_path, artifact, run_id=7, run_attempt=2)
    assert rc == 0
    first = (out / "comment.md").read_text().split("\n", 1)[0]
    assert first == _marker(run=7, attempt=2, outcome="evidence")


def test_refusal_stamps_the_notice_marker(tmp_path, capsys):
    (tmp_path / "refusal.json").write_text(json.dumps({"reason": "too_large"}))
    rc = publish_evidence.main(
        [
            "refusal",
            "--reason-file", str(tmp_path / "refusal.json"),
            "--head-sha", HEAD_SHA,
            "--run-id", "7",
            "--run-attempt", "2",
        ]
    )
    assert rc == 0
    first = capsys.readouterr().out.split("\n", 1)[0]
    assert first == _marker(run=7, attempt=2, outcome="notice")


def test_refusal_without_marker_args_keeps_the_bare_marker(tmp_path, capsys):
    (tmp_path / "refusal.json").write_text(json.dumps({"reason": "too_large"}))
    rc = publish_evidence.main(
        ["refusal", "--reason-file", str(tmp_path / "refusal.json")]
    )
    assert rc == 0
    assert capsys.readouterr().out.startswith(evidence_checks.REFUSAL_MARKER)


def test_comment_job_is_serialised_per_pr():
    # The comment-writing job — and only that job — carries a concurrency
    # group keyed by the PR number from resolve's outputs, waiting instead
    # of cancelling, so one PR's publishers never write at once.
    text = (
        ROOT / ".github" / "workflows" / "evidence-publish.yml"
    ).read_text(encoding="utf-8")
    comment_at = text.index("\n  comment:")
    assert text.count("cancel-in-progress") == 1
    group = "group: evidence-comment-${{ needs.resolve.outputs.pr }}"
    assert group in text[comment_at:]
    assert "cancel-in-progress: false" in text[comment_at:]


def _evidence_comment(head=HEAD_SHA, run=100, attempt=1):
    return _marker(head, run, attempt, "evidence") + "\n## Before / after evidence\n"


def test_comment_step_patches_the_existing_marker_comment(tmp_path, monkeypatch):
    # Same head, earlier run already posted: the new run patches that
    # comment instead of posting a duplicate.
    listing = [_bot_comment(11, run=90)]
    files = {"rendered/comment.md": _evidence_comment() + "body\n"}
    proc, calls = _run_comment_step(
        tmp_path, monkeypatch, "success", "success",
        files=files, listing=listing, evidence_sha="c" * 40,
    )
    assert proc.returncode == 0, proc.stderr
    writes = [c for c in calls if c["method"] in ("POST", "PATCH", "DELETE")]
    assert len(writes) == 1, writes
    assert writes[0]["method"] == "PATCH"
    url = next(a for a in writes[0]["argv"] if "repos/" in a)
    assert url == f"repos/{REPO}/issues/comments/11"


def test_comment_step_skips_a_stale_notice_over_newer_evidence(tmp_path, monkeypatch):
    # Interleaving (b) end to end: good evidence from run 200 is already
    # posted; this run 100 only has a publish_failed notice. It writes
    # nothing.
    listing = [_bot_comment(11, run=200, outcome="evidence")]
    proc, calls = _run_comment_step(
        tmp_path, monkeypatch, "success", "failure", listing=listing,
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == [], calls


def test_comment_step_stops_when_the_head_moved(tmp_path, monkeypatch):
    # Interleaving (c) end to end: this run recorded H1 but the PR now
    # points at H2, so the freshness re-check stops the step before any
    # lookup or write, and the H2 comment survives untouched.
    listing = [_bot_comment(11, head=OTHER_HEAD, run=101)]
    files = {"rendered/comment.md": _evidence_comment() + "body\n"}
    proc, calls = _run_comment_step(
        tmp_path, monkeypatch, "success", "success",
        files=files, listing=listing, evidence_sha="c" * 40,
        fake_head=OTHER_HEAD,
    )
    assert proc.returncode == 0, proc.stderr
    assert calls == [], calls


def test_comment_step_replaces_a_stale_head_and_dedupes(tmp_path, monkeypatch):
    # Fresh head, two leftover marker comments: patch one, delete the other.
    listing = [
        _bot_comment(11, head=OTHER_HEAD, run=90),
        _bot_comment(12, head=OTHER_HEAD, run=95),
    ]
    files = {"rendered/comment.md": _evidence_comment() + "body\n"}
    proc, calls = _run_comment_step(
        tmp_path, monkeypatch, "success", "success",
        files=files, listing=listing, evidence_sha="c" * 40,
    )
    assert proc.returncode == 0, proc.stderr
    methods = sorted(c["method"] for c in calls)
    assert methods == ["DELETE", "PATCH"], calls
    patch = next(c for c in calls if c["method"] == "PATCH")
    drop = next(c for c in calls if c["method"] == "DELETE")
    patch_url = next(a for a in patch["argv"] if "repos/" in a)
    drop_url = next(a for a in drop["argv"] if "repos/" in a)
    assert patch_url.endswith("/12")
    assert drop_url.endswith("/11")
