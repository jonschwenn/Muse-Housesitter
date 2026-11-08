"""Shared fixtures for the module conformance suite."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import harness
import pytest


@pytest.fixture(scope="session")
def all_runs(tmp_path_factory):
    """Run every manifest case for every module under test, once.

    Returns ``{module_id: {cls, manifest, fixture_dir, sessions, runs,
    follows}}`` where ``runs`` maps case names to ``SingleRun`` and
    ``follows`` maps truncated case names to their ``more`` follow-ups.
    Independent cases get independent temp HOMEs (so one case's saved
    files can never leak into another's scan); a snapshot and its
    follow-ups share one session. ``session`` is kept as an alias for
    the first case's session for checks that only need a registry and
    a context.
    """
    out = {}
    for cls, fixture_dir in harness.modules_under_test():
        manifest = harness.load_manifest(cls.id, fixture_dir)
        base = tmp_path_factory.mktemp(cls.id)
        sessions = {}
        runs = {}
        follows = {}
        for case in manifest["cases"]:
            session = harness.CaseSession(
                cls, manifest, base / case["name"], fixture_dir
            )
            sessions[case["name"]] = session
            first = session.run_case(case["name"])
            runs[case["name"]] = first
            if isinstance(first.outcome.envelope.get("more"), dict):
                follows[case["name"]] = session.follow_paging(first)
        first_case = manifest["cases"][0]["name"]
        out[cls.id] = {
            "cls": cls,
            "manifest": manifest,
            "fixture_dir": fixture_dir,
            "sessions": sessions,
            "session": sessions[first_case],
            "runs": runs,
            "follows": follows,
        }
    return out
