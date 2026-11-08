"""The test-only demo module: never installed, pinned normalisation."""

from pathlib import Path

import harness
from demo_module import ConformanceDemo

from housesitter.core.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_ROOT = REPO_ROOT / "src" / "housesitter"
FIXTURE_DIR = harness.FIXTURES_ROOT / "conformance_demo"


def test_demo_module_is_not_a_builtin():
    """``load_registry()`` has no ``conformance_demo`` command."""
    registry = load_registry(include_entry_points=False)
    assert ConformanceDemo.id not in [entry.id for entry in registry.modules]
    assert not [
        name
        for name in registry.commands
        if name.startswith("housesitter.conformance_demo.")
    ]


def test_nothing_under_src_references_the_demo_module():
    """No source file may import or name the test-only module."""
    hits = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "conformance_demo" in text:
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == []


def _run(tmp_path, case_name):
    manifest = harness.load_manifest("conformance_demo", FIXTURE_DIR)
    session = harness.CaseSession(ConformanceDemo, manifest, tmp_path, FIXTURE_DIR)
    return session.run_case(case_name)


def test_snapshot_typical_normalisation(tmp_path):
    run = _run(tmp_path, "snapshot-typical")
    assert run.outcome.exit_code == 0
    data = run.outcome.envelope["data"]
    assert data["sensor_count"] == 4
    assert data["sensors"][0] == {
        "id": "02:00:00:00:00:01",
        "name": "Living Room",
        "ip": "192.0.2.11",
        "ssid": "home-example",
        "temperature_c": 21.5,
        "humidity_pct": 45,
        "online": True,
    }
    assert data["sensors"][1]["temperature_c"] is None
    assert data["sensors"][2]["humidity_pct"] is None
    assert data["sensors"][3]["temperature_c"] is None
    assert run.outcome.envelope["source"] == {
        "kind": "sensor_hub",
        "api_version": "1.2.3",
    }
    assert run.outcome.envelope["warnings"] == ["2 sensors reported no temperature"]
    assert "x_passphrase" not in run.stdout_text


def test_snapshot_empty_normalisation(tmp_path):
    run = _run(tmp_path, "snapshot-empty")
    assert run.outcome.exit_code == 0
    assert run.outcome.envelope["data"] == {"sensors": [], "sensor_count": 0}
    assert run.outcome.envelope["warnings"] == []


def test_status_typical_normalisation(tmp_path):
    run = _run(tmp_path, "status-typical")
    assert run.outcome.exit_code == 0
    assert run.outcome.envelope["data"] == {
        "firmware": "hub-fw 1.2.3",
        "uptime_s": 86400,
        "sensor_count": 4,
    }
