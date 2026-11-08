"""The module conformance suite: every check over every module under test.

Each test asserts a check reports no problems AND examined > 0 items,
so a check that silently looks at nothing cannot pass. Per-case
examined counts are aggregated per module where a single case may
legitimately contribute nothing (an error envelope has no warnings).
Every per-run check runs over each case's first run AND every
follow-up page: a fault that rides only in ``more`` pages must still
be caught.
"""

import json
from pathlib import Path

import checks
import harness
import pytest
from harness import load_manifest, planted_strings

from housesitter.core.registry import Registry, load_registry

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_ENTRIES = harness.modules_under_test()
_MODULE_IDS = [cls.id for cls, _ in _ENTRIES]

_CASES = []
_HOSTILE_CASES = []
for _cls, _fdir in _ENTRIES:
    _manifest = load_manifest(_cls.id, _fdir)
    for _case in _manifest["cases"]:
        _CASES.append(
            pytest.param(
                _cls.id, _case["name"], id=f"{_cls.id}/{_case['name']}"
            )
        )
        if _case["fixture"] == "hostile":
            _HOSTILE_CASES.append(
                pytest.param(
                    _cls.id, _case["name"], id=f"{_cls.id}/{_case['name']}"
                )
            )


def _entry(all_runs, module_id):
    return all_runs[module_id]


def _case_leaves(entry, case_name):
    """JSON string leaves from one case's fixture bodies."""
    for case in entry["manifest"]["cases"]:
        if case["name"] != case_name:
            continue
        return harness.case_string_leaves(case, entry["fixture_dir"])
    raise AssertionError(f"unknown case {case_name!r}")


def _pages(entry, case_name):
    """One case's first run plus every follow-up page, in order."""
    return harness.page_runs(entry, case_name)


class TestModules:
    def test_modules_under_test_is_nonempty_and_has_demo(self, all_runs):
        result = checks.check_module_list(harness.modules_under_test())
        assert result.problems == []
        assert result.examined > 0
        assert set(all_runs) == set(_MODULE_IDS)

    def test_every_fixture_kind_is_covered(self, all_runs):
        for module_id in _MODULE_IDS:
            kinds = {
                case["fixture"] for case in all_runs[module_id]["manifest"]["cases"]
            }
            for kind in ("typical", "empty", "error", "large", "hostile"):
                assert kind in kinds, f"{module_id} has no {kind} case"


class TestNames:
    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_names(self, all_runs, module_id):
        result = checks.check_names(all_runs[module_id]["cls"])
        assert result.problems == []
        assert result.examined > 0


class TestDropins:
    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_dropins(self, all_runs, module_id):
        registry = Registry.from_classes([all_runs[module_id]["cls"]])
        result = checks.check_dropins(registry)
        assert result.problems == []
        assert result.examined > 0


class TestEnvelope:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_envelope(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        problems = []
        examined = 0
        for run in _pages(entry, case_name):
            result = checks.check_envelope(run)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []
        assert examined > 0


class TestStreams:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_no_stray_output(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        problems = []
        examined = 0
        for run in _pages(entry, case_name):
            result = checks.check_stdio(run)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []
        assert examined > 0


class TestSizePaging:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_size_and_paging(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        first = entry["runs"][case_name]
        follows = entry["follows"].get(case_name, [])
        result = checks.check_size_paging(first, follows, entry["session"].registry)
        assert result.problems == []
        assert result.examined > 0

    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_large_and_hostile_truncate(self, all_runs, module_id):
        """A module that returns lists has a large and a hostile case that
        really truncates. Output without list fields has no unbounded part
        to page; a hostile case for a command that never pages may expect
        "ok"."""
        cls = all_runs[module_id]["cls"]
        if not checks.module_has_lists(cls):
            pytest.skip(f"{module_id} declares no list fields in data")
        runs = all_runs[module_id]["runs"].values()
        for kind in ("large", "hostile"):
            truncated = [
                run for run in runs
                if run.case["fixture"] == kind and run.case["expect"] == "truncated"
            ]
            assert truncated, f"{module_id} has no truncating {kind} case"
            for run in truncated:
                assert run.outcome.envelope.get("truncated") is True
                assert isinstance(run.outcome.envelope.get("more"), dict)


class TestHttp:
    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_http(self, all_runs, module_id):
        entry = all_runs[module_id]
        runs = [
            run
            for name in entry["runs"]
            for run in _pages(entry, name)
        ]
        result = checks.check_http(entry["cls"], entry["session"], runs)
        assert result.problems == []
        assert result.examined > 0


class TestHostileNames:
    @pytest.mark.parametrize("module_id,case_name", _HOSTILE_CASES)
    def test_hostile_names(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        pages = _pages(entry, case_name)
        problems = []
        examined = 0
        for pos, run in enumerate(pages):
            result = checks.check_hostile_names(run, require_dirty=(pos == 0))
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []
        assert examined > 0

    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_every_case_is_sanitised(self, all_runs, module_id, case_name):
        """Sanitisation holds on every successful case, not only those
        labelled hostile; only hostile cases must also hold dirty input."""
        entry = all_runs[module_id]
        run = entry["runs"][case_name]
        if run.case["expect"] not in ("ok", "truncated"):
            pytest.skip("error envelopes carry no data")
        problems = []
        examined = 0
        for page in _pages(entry, case_name):
            result = checks.check_hostile_names(page, require_dirty=False)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []


class TestSecrets:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_no_planted_secrets(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        planted = planted_strings(entry["manifest"], entry["fixture_dir"])
        assert planted, f"{module_id} plants no secrets; the check is vacuous"
        problems = []
        examined = 0
        for run in _pages(entry, case_name):
            result = checks.check_secrets(run, planted)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []
        assert examined > 0


class TestFields:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_declared_fields(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        run = entry["runs"][case_name]
        if run.case["expect"] not in ("ok", "truncated"):
            pytest.skip("error envelopes carry no data")
        problems = []
        examined = 0
        for page in _pages(entry, case_name):
            result = checks.check_fields(page)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []
        assert examined > 0


class TestSpec:
    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_spec(self, all_runs, module_id):
        entry = all_runs[module_id]
        spec_path = REPO_ROOT / entry["manifest"]["spec"]
        result = checks.check_spec(entry["cls"], spec_path)
        assert result.problems == []
        assert result.examined > 0


class TestWarningsParams:
    @pytest.mark.parametrize("module_id,case_name", _CASES)
    def test_warnings_and_params(self, all_runs, module_id, case_name):
        entry = all_runs[module_id]
        run = entry["runs"][case_name]
        if run.case["expect"] not in ("ok", "truncated"):
            pytest.skip("error envelopes carry no warnings")
        leaves = _case_leaves(entry, case_name)
        planted = planted_strings(entry["manifest"], entry["fixture_dir"])
        problems = []
        examined = 0
        for page in _pages(entry, case_name):
            result = checks.check_warnings_params(page, leaves, planted)
            problems.extend(result.problems)
            examined += result.examined
        assert problems == []

    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_warnings_params_is_not_vacuous(self, all_runs, module_id):
        entry = all_runs[module_id]
        planted = planted_strings(entry["manifest"], entry["fixture_dir"])
        total = 0
        for name, run in entry["runs"].items():
            if run.case["expect"] not in ("ok", "truncated"):
                continue
            for page in _pages(entry, name):
                total += checks.check_warnings_params(
                    page, _case_leaves(entry, name), planted
                ).examined
        if total == 0:
            # Nothing was examined: prove there was truly nothing to see,
            # rather than failing a quiet module (round-3 finding 5).
            for run in entry["runs"].values():
                env = run.outcome.envelope
                if env.get("ok") is True:
                    assert env.get("warnings") == [], run.full_name
                    assert env.get("more") is None, run.full_name


class TestSkill:
    @pytest.mark.parametrize("module_id", _MODULE_IDS)
    def test_skill(self, all_runs, module_id):
        entry = all_runs[module_id]
        planted = planted_strings(entry["manifest"], entry["fixture_dir"])
        result = checks.check_skill(all_runs[module_id]["cls"], planted)
        assert result.problems == []
        assert result.examined > 0


class TestEvidence:
    def test_every_command_has_a_scenario(self):
        """Live run: the registry holds no module commands yet, so this
        passes with 0 examined. That is expected today (only core
        commands have scenarios); its correctness is proven by the
        mutation test that adds a command and a scenario list."""
        scenarios_path = REPO_ROOT / "tests" / "evidence" / "scenarios.json"
        scenarios = json.loads(scenarios_path.read_text(encoding="utf-8"))[
            "scenarios"
        ]
        registry = load_registry(include_entry_points=False)
        result = checks.check_evidence(registry, scenarios)
        assert result.problems == []
