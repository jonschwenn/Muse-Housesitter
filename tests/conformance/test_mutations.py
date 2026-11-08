"""Mutation tests: each check catches the fault it guards against.

Every test applies one mutation (a module subclass, a monkeypatched core
function, an edited spec/SKILL/scenario copy, or a temp source file),
runs the relevant check, and asserts it reports a problem. Each test
also runs the unmutated control and asserts it passes.
"""

import copy
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import checks
import harness
import pytest
from demo_module import ConformanceDemo
from test_imports import core_files, scan_core_file, scan_module_file

from housesitter import sdk
from housesitter.core import runner
from housesitter.core.registry import Registry

FIXTURE_DIR = harness.FIXTURES_ROOT / "conformance_demo"
SPEC_PATH = Path(__file__).resolve().parent / "demo_module" / "SPEC.md"
SKILL_PATH = Path(__file__).resolve().parent / "demo_module" / "SKILL.md"

LEAK = "PLANTED-SECRET-typical-1"


def fresh_manifest():
    """A deep copy of the demo manifest for mutation."""
    return json.loads(
        json.dumps(harness.load_manifest("conformance_demo", FIXTURE_DIR))
    )


def session_for(cls, manifest, tmp_path):
    """A harness session for a (possibly mutated) module class."""
    return harness.CaseSession(cls, manifest, tmp_path, FIXTURE_DIR)


def planted(manifest):
    found = harness.planted_strings(manifest, FIXTURE_DIR)
    assert LEAK in found
    return found


def fetch_payload(ctx, base_url):
    """Read the sensor payload through a live context (test helper)."""
    with ctx.http(base_url) as live:
        return live.get("/api/v1/sensors-example").json()


class OffListGet(ConformanceDemo):
    def snapshot(self, ctx, params):
        with ctx.http(ctx.config["base_url"], tls=ctx.config.get("tls")) as live:
            live.get("/api/other-example")
        return sdk.Result(
            data={"sensors": [], "sensor_count": 0},
            source={"kind": "sensor_hub", "api_version": None},
        )


def test_off_list_get_is_caught_by_http_check(tmp_path):
    """allow-list bypass by path trick -> the http check."""
    manifest = fresh_manifest()
    session = session_for(OffListGet, manifest, tmp_path / "bad")
    bad = session.run_case("snapshot-typical")
    assert bad.outcome.error_code == "not_allowed"
    assert checks.check_http(OffListGet, session, [bad]).problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-typical")
    assert checks.check_http(ConformanceDemo, good_session, [good]).problems == []


class DeleteWrite(ConformanceDemo):
    def snapshot(self, ctx, params):
        with ctx.http(ctx.config["base_url"], tls=ctx.config.get("tls")) as live:
            live.request("DELETE", "/api/v1/sensors-example")
        return sdk.Result(
            data={"sensors": [], "sensor_count": 0},
            source={"kind": "sensor_hub", "api_version": None},
        )


def test_delete_is_caught_by_http_check(tmp_path):
    """a module write to upstream -> the http check."""
    manifest = fresh_manifest()
    session = session_for(DeleteWrite, manifest, tmp_path / "bad")
    bad = session.run_case("snapshot-typical")
    assert bad.outcome.error_code == "not_allowed"
    result = checks.check_http(DeleteWrite, session, [bad])
    assert result.problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-typical")
    assert checks.check_http(ConformanceDemo, good_session, [good]).problems == []


class ExtraKey(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        data = dict(result.data)
        data["sneaky"] = 1
        return sdk.Result(
            data=data,
            source=result.source,
            compact=result.compact,
            paging=result.paging,
            warnings=result.warnings,
            label=result.label,
            save=result.save,
        )


def test_undeclared_key_is_caught_by_fields_check(tmp_path):
    """handler passes through an undeclared key -> declared-fields (b)."""
    manifest = fresh_manifest()
    bad = session_for(ExtraKey, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert checks.check_fields(bad).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_fields(good).problems == []


def spec_without_row(tmp_path):
    text = SPEC_PATH.read_text(encoding="utf-8")
    matches = [
        line
        for line in text.splitlines()
        if line.startswith("| `sensor_count` |")
    ]
    assert len(matches) == 1
    target = tmp_path / "SPEC.md"
    target.write_text(text.replace(matches[0] + "\n", ""), encoding="utf-8")
    return target


def test_deleted_spec_row_is_caught_by_spec_check(tmp_path):
    """a spec row is deleted -> the spec check."""
    result = checks.check_spec(ConformanceDemo, spec_without_row(tmp_path))
    assert result.problems != []

    assert checks.check_spec(ConformanceDemo, SPEC_PATH).problems == []


def spec_with_extra_row(tmp_path):
    text = SPEC_PATH.read_text(encoding="utf-8")
    row = (
        "| `api_version` | string\\|null | — | `api_version` | "
        "Hub API version, as reported. **Untrusted** (capped at 32). |"
    )
    assert row in text
    extra = row + "\n| `bogus` | string | — | nowhere | No such field. |"
    target = tmp_path / "SPEC.md"
    target.write_text(text.replace(row, extra), encoding="utf-8")
    return target


def test_extra_spec_row_is_caught_by_spec_check(tmp_path):
    """an extra spec row is added -> the spec check."""
    result = checks.check_spec(ConformanceDemo, spec_with_extra_row(tmp_path))
    assert result.problems != []

    assert checks.check_spec(ConformanceDemo, SPEC_PATH).problems == []


class NoPaging(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=None,
            warnings=result.warnings,
            label=result.label,
            save=result.save,
        )


def test_missing_paging_is_caught_by_envelope_check(tmp_path):
    """the Paging is removed while output is oversized -> the expect check."""
    manifest = fresh_manifest()
    bad = session_for(NoPaging, manifest, tmp_path / "bad").run_case("snapshot-large")
    result = checks.check_envelope(bad)
    assert result.problems != []
    assert bad.outcome.envelope.get("ok") is False

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-large"
    )
    assert checks.check_envelope(good).problems == []


def test_patched_envelope_fits_is_caught_by_size_check(tmp_path, monkeypatch):
    """runner._envelope_fits patched True while oversized -> the size check."""
    manifest = fresh_manifest()
    session = session_for(ConformanceDemo, manifest, tmp_path / "bad")
    monkeypatch.setattr(runner, "_envelope_fits", lambda env: True)
    bad = session.run_case("snapshot-large")
    assert bad.outcome.envelope.get("truncated") is False
    result = checks.check_size_paging(bad, [], session.registry)
    assert result.problems != []

    monkeypatch.undo()
    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-large")
    follows = good_session.follow_paging(good)
    assert checks.check_size_paging(good, follows, good_session.registry).problems == []


class SkippingGet(ConformanceDemo):
    def get(self, ctx, params):
        shifted = dict(params)
        shifted["offset"] = params.get("offset", 0) + 1
        return super().get(ctx, shifted)


def test_broken_offset_is_caught_by_paging_follow(tmp_path):
    """a broken more.offset skips items -> the paging-follow check."""
    manifest = fresh_manifest()
    session = session_for(SkippingGet, manifest, tmp_path / "bad")
    first = session.run_case("snapshot-large")
    assert first.outcome.envelope.get("truncated") is True
    follows = session.follow_paging(first)
    result = checks.check_size_paging(first, follows, session.registry)
    assert result.problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-large")
    good_follows = good_session.follow_paging(good)
    assert checks.check_size_paging(
        good, good_follows, good_session.registry
    ).problems == []


class DataLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        sensors = [dict(item) for item in result.data["sensors"]]
        sensors[0]["name"] = LEAK
        return sdk.Result(
            data={"sensors": sensors, "sensor_count": result.data["sensor_count"]},
            source=result.source,
            compact=result.compact,
            paging=result.paging,
            warnings=result.warnings,
            label=result.label,
            save=result.save,
        )


def test_secret_leaked_into_data_is_caught(tmp_path):
    """a PLANTED-SECRET is leaked into data -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(DataLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class WarningLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=result.paging,
            warnings=["hub note " + LEAK],
            label=result.label,
            save=result.save,
        )


def test_secret_leaked_into_warning_is_caught(tmp_path):
    """a PLANTED-SECRET is leaked into a warning -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(WarningLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class LogLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        payload = fetch_payload(ctx, ctx.config["base_url"])
        leaked = payload["sensors"][0]["x_passphrase"]
        ctx.log.info("fetched sensor debug %s", leaked)
        return result


def test_secret_leaked_into_log_is_caught(tmp_path):
    """a PLANTED-SECRET is leaked into a log line -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(LogLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert any(LEAK in record for record in bad.log_records)
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class ReasonLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        raise sdk.CommandError("upstream_unexpected", "hub said " + LEAK)


def test_secret_leaked_into_error_is_caught(tmp_path):
    """a PLANTED-SECRET is leaked into a CommandError -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(ReasonLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert bad.outcome.exit_code == 1
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-error")
    assert checks.check_secrets(good, planted(manifest)).problems == []


class MacParams(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=sdk.Paging(
                command="get",
                lists=("sensors",),
                params={"debug_mac": "02:00:00:00:00:09"},
            ),
            warnings=result.warnings,
            label=result.label,
            save=result.save,
        )


def test_mac_in_paging_params_is_caught(tmp_path):
    """a MAC in Paging.params -> the warnings/params check."""
    manifest = fresh_manifest()
    bad = session_for(MacParams, manifest, tmp_path / "bad").run_case("snapshot-large")
    case = next(
        item for item in manifest["cases"] if item["name"] == "snapshot-large"
    )
    leaves = harness.case_string_leaves(case, FIXTURE_DIR)
    assert checks.check_warnings_params(bad, leaves, planted(manifest)).problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-typical")
    typical = next(
        item for item in manifest["cases"] if item["name"] == "snapshot-typical"
    )
    assert checks.check_warnings_params(
        good, harness.case_string_leaves(typical, FIXTURE_DIR), planted(manifest)
    ).problems == []


class SsidWarning(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=result.paging,
            warnings=["home-example is congested"],
            label=result.label,
            save=result.save,
        )


def test_ssid_in_warning_is_caught(tmp_path):
    """an SSID in a warning -> the warnings/params check."""
    manifest = fresh_manifest()
    bad = session_for(SsidWarning, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    case = next(
        item for item in manifest["cases"] if item["name"] == "snapshot-typical"
    )
    leaves = harness.case_string_leaves(case, FIXTURE_DIR)
    assert "home-example" in leaves
    assert checks.check_warnings_params(bad, leaves, planted(manifest)).problems != []

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-typical")
    assert checks.check_warnings_params(
        good, leaves, planted(manifest)
    ).problems == []


def test_disabled_sanitiser_is_caught_by_hostile_check(tmp_path, monkeypatch):
    """the sanitiser is disabled for one field -> the hostile-names check."""
    manifest = fresh_manifest()
    session = session_for(ConformanceDemo, manifest, tmp_path / "bad")
    real_sanitize = runner.sanitize_mapping

    def skip_names(data, fields):
        kept = [item for item in fields if not item.path.endswith(".name")]
        return real_sanitize(data, kept)

    monkeypatch.setattr(runner, "sanitize_mapping", skip_names)
    bad = session.run_case("snapshot-hostile")
    assert checks.check_hostile_names(bad).problems != []

    monkeypatch.undo()
    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-hostile")
    assert checks.check_hostile_names(good).problems == []


def skill_module(tmp_path, skill_text, modname):
    """A throwaway package whose SKILL.md reads ``skill_text``."""
    package = tmp_path / modname
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "SKILL.md").write_text(skill_text, encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        __import__(modname)
    finally:
        sys.path.remove(str(tmp_path))
    return type("SkillMod", (ConformanceDemo,), {"__module__": modname})


def skill_without_command(tmp_path):
    text = SKILL_PATH.read_text(encoding="utf-8")
    missing = "housesitter.conformance_demo.get"
    assert missing in text
    return skill_module(
        tmp_path, text.replace(missing, "housesitter.conformance_demo.got"), "skill_noget"
    )


def test_skill_missing_command_is_caught(tmp_path):
    """SKILL.md is missing a command -> the skill check."""
    assert checks.check_skill(skill_without_command(tmp_path)).problems != []
    assert checks.check_skill(ConformanceDemo).problems == []


def skill_without_name(tmp_path):
    text = SKILL_PATH.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("name:")]
    assert len(lines) < len(text.splitlines())
    return skill_module(tmp_path, "\n".join(lines) + "\n", "skill_noname")


def test_skill_missing_name_is_caught(tmp_path):
    """SKILL.md is missing name -> the skill check."""
    assert checks.check_skill(skill_without_name(tmp_path)).problems != []
    assert checks.check_skill(ConformanceDemo).problems == []


def crafted_module(module_id, command_names):
    """A module-shaped class bypassing Command validation.

    Real ``Command`` objects reject bad names at construction, so a
    mutation that must reach ``check_names`` needs a crafted stand-in
    with the same ``id``/``commands``/``name`` shape.
    """
    return type(
        "CraftedModule",
        (),
        {
            "id": module_id,
            "commands": tuple(
                SimpleNamespace(name=name) for name in command_names
            ),
        },
    )


def test_overlong_full_name_is_caught_by_names_check():
    """a full command name over 64 chars -> the names check."""
    bad = crafted_module("a" * 60, ["snapshot"])
    assert len(f"housesitter.{bad.id}.snapshot") > 64
    problems = checks.check_names(bad).problems
    assert any("over 64" in problem for problem in problems)

    result = checks.check_names(ConformanceDemo)
    assert result.problems == []
    assert result.examined > 0


def test_invalid_command_name_is_caught_by_names_check():
    """a command name outside [a-z][a-z0-9_]* -> the names check."""
    bad = crafted_module("conformance_demo", ["Bad Name"])
    problems = checks.check_names(bad).problems
    assert any("invalid command name" in problem for problem in problems)

    result = checks.check_names(ConformanceDemo)
    assert result.problems == []
    assert result.examined > 0


def mutated_dropins(monkeypatch, mutate):
    """Patch drop-in generation to return one mutated mapping."""
    from housesitter.core import dropins as dropins_module

    registry = Registry.from_classes([ConformanceDemo])
    real = dropins_module.generate(registry, "/opt/housesitter/bin/housesitter")
    bad_files = copy.deepcopy(real)
    mutate(bad_files)
    monkeypatch.setattr(
        dropins_module, "generate", lambda *args, **kwargs: bad_files
    )
    return registry


def test_extra_dropin_key_is_caught(tmp_path, monkeypatch):
    """a generated drop-in carrying an extra key -> the drop-ins check."""
    del tmp_path

    def add_key(files):
        first = sorted(files)[0]
        files[first] = dict(files[first], bogus=1)

    registry = mutated_dropins(monkeypatch, add_key)
    problems = checks.check_dropins(registry).problems
    assert any("unknown key" in problem for problem in problems)

    monkeypatch.undo()
    result = checks.check_dropins(registry)
    assert result.problems == []
    assert result.examined > 0


def test_bad_dropin_param_spec_is_caught(tmp_path, monkeypatch):
    """a generated drop-in with a bad param spec -> the drop-ins check."""
    del tmp_path

    def break_spec(files):
        for name in sorted(files):
            required = files[name].get("required", {})
            if required:
                param = sorted(required)[0]
                files[name]["required"][param] = {"type": "string"}
                return
        raise AssertionError("no drop-in has a required param to break")

    registry = mutated_dropins(monkeypatch, break_spec)
    problems = checks.check_dropins(registry).problems
    assert any("exactly type and description" in problem for problem in problems)

    monkeypatch.undo()
    result = checks.check_dropins(registry)
    assert result.problems == []
    assert result.examined > 0


def test_command_without_scenario_is_caught():
    """a registered command has no scenario -> the evidence check."""
    registry = Registry.from_classes([ConformanceDemo])
    scenarios = [
        {"argv": ["invoke", "housesitter.conformance_demo.snapshot"]}
    ]
    assert checks.check_evidence(registry, scenarios).problems != []

    full = [
        {"argv": ["invoke", f"housesitter.conformance_demo.{name}"]}
        for name in ("snapshot", "get", "status")
    ]
    assert checks.check_evidence(registry, full).problems == []


def write_source(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_import_socket_is_caught(tmp_path):
    """import socket -> the import lint."""
    bad = write_source(tmp_path, "bad_socket.py", "import socket\n")
    assert scan_module_file(bad, "demo_module") != []

    good = write_source(tmp_path, "good.py", "import json\n")
    assert scan_module_file(good, "demo_module") == []


def test_import_http_client_is_caught(tmp_path):
    """from http import client -> the import lint."""
    bad = write_source(tmp_path, "bad_http.py", "from http import client\n")
    assert scan_module_file(bad, "demo_module") != []

    good = write_source(tmp_path, "good.py", "import json\n")
    assert scan_module_file(good, "demo_module") == []


def test_import_core_http_is_caught(tmp_path):
    """import housesitter.core.http -> the import lint."""
    bad = write_source(tmp_path, "bad_core.py", "import housesitter.core.http\n")
    assert scan_module_file(bad, "demo_module") != []

    good = write_source(tmp_path, "good.py", "from housesitter import sdk\n")
    assert scan_module_file(good, "demo_module") == []


def test_import_importlib_is_caught(tmp_path):
    """importlib.import_module(...) -> the import lint."""
    bad = write_source(
        tmp_path, "bad_dynamic.py", "import importlib\nimportlib.import_module('os')\n"
    )
    assert scan_module_file(bad, "demo_module") != []

    good = write_source(tmp_path, "good.py", "import json\n")
    assert scan_module_file(good, "demo_module") == []


def test_core_importing_modules_is_caught(tmp_path):
    """a core file importing housesitter.modules.x -> the import lint."""
    bad = write_source(
        tmp_path, "bad_core.py", "import housesitter.modules.conformance_demo\n"
    )
    assert scan_core_file(bad) != []

    good = write_source(tmp_path, "good_core.py", "import os\n")
    assert scan_core_file(good) == []
    assert core_files() != []


def test_empty_module_list_is_caught():
    """an empty module list -> the non-vacuity guard function."""
    assert checks.check_module_list([]).problems != []

    assert (
        checks.check_module_list(harness.modules_under_test()).problems == []
    )


def test_bare_import_http_is_caught(tmp_path):
    """import http (exposes http.client once the core is loaded) -> the import lint."""
    for name, source in (
        ("bare.py", "import http\n"),
        ("server.py", "import http.server\n"),
        ("cookies.py", "from http import cookies\n"),
        ("requests_mod.py", "import requests\n"),
    ):
        bad = write_source(tmp_path, name, source)
        assert scan_module_file(bad, "demo_module") != [], source

    good = write_source(tmp_path, "good.py", "import json\nfrom housesitter import sdk\n")
    assert scan_module_file(good, "demo_module") == []


class ForgedPagingId(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=sdk.Paging(
                command="get", lists=("sensors",), params={"id": "kitchen-example"}
            ),
            warnings=result.warnings,
            label=result.label,
        )


def test_non_store_paging_id_is_caught_by_paging_check(tmp_path):
    """more.params id is module text, not a store id -> the size/paging check."""
    manifest = fresh_manifest()
    session = session_for(ForgedPagingId, manifest, tmp_path / "bad")
    bad = session.run_case("snapshot-large")
    assert bad.outcome.envelope["truncated"] is True
    assert bad.outcome.envelope["more"]["params"]["id"] == "kitchen-example"
    problems = checks.check_size_paging(bad, [], session.registry).problems
    assert any("not a store id" in problem for problem in problems)

    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    good = good_session.run_case("snapshot-large")
    good_problems = checks.check_size_paging(
        good, good_session.follow_paging(good), good_session.registry
    ).problems
    assert not any("not a store id" in problem for problem in good_problems)


def test_unsaved_self_paging_passes_paging_check(tmp_path):
    """control: a get page that pages itself without saving is accepted."""
    manifest = fresh_manifest()
    session = session_for(ConformanceDemo, manifest, tmp_path)
    first = session.run_case("snapshot-hostile")
    follows = session.follow_paging(first)
    assert len(follows) >= 2, "need a get page that is itself truncated"
    page = follows[0]
    assert page.outcome.envelope["saved"] is None
    assert page.outcome.envelope["truncated"] is True
    result = checks.check_size_paging(page, follows[1:], session.registry)
    assert result.problems == []
    assert result.examined > 0


class CommonWordWarning(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return sdk.Result(
            data=result.data,
            source=result.source,
            compact=result.compact,
            paging=result.paging,
            warnings=["sensor list read in one request; online values are as reported"],
            label=result.label,
            save=result.save,
        )


def test_common_word_warning_is_not_flagged(tmp_path):
    """control: author text sharing plain words with the fixture is not upstream text."""
    manifest = fresh_manifest()
    run = session_for(CommonWordWarning, manifest, tmp_path).run_case("snapshot-typical")
    case = next(i for i in manifest["cases"] if i["name"] == "snapshot-typical")
    # Plain words a future fixture could hold, and that this warning repeats.
    words = {"sensor", "request", "online"}
    leaves = harness.case_string_leaves(case, FIXTURE_DIR) | words
    assert all(word in run.outcome.envelope["warnings"][0] for word in words)
    assert checks.check_warnings_params(run, leaves, planted(manifest)).problems == []
    # Distinctive upstream text in the same warning is still caught.
    leaves_with_name = leaves | {"Living Room"}
    run.outcome.envelope["warnings"].append("Living Room is offline")
    assert checks.check_warnings_params(
        run, leaves_with_name, planted(manifest)
    ).problems != []


class BareLogout(ConformanceDemo):
    def status(self, ctx, params):
        # The pre-fix pattern: a logout that can raise inside ``finally``.
        result = super().status(ctx, params)
        with ctx.http(ctx.config["base_url"]) as session:
            session.post("/api/logout")
        return result


def test_raising_logout_is_caught_by_envelope_check(tmp_path):
    """a logout 401 that escapes -> the envelope check (status-logout-401 expects ok)."""
    manifest = fresh_manifest()
    bad = session_for(BareLogout, manifest, tmp_path / "bad").run_case("status-logout-401")
    assert bad.outcome.error_code == "upstream_auth"
    assert checks.check_envelope(bad).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "status-logout-401"
    )
    assert good.outcome.exit_code == 0
    assert checks.check_envelope(good).problems == []


class ShortNameWarning(ConformanceDemo):
    def __init__(self, warning="sensor list read in one request; online values are as reported"):
        self.warning = warning

    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        data = json.loads(json.dumps(dict(result.data)))
        data["sensors"][0]["name"] = "on"
        return sdk.Result(
            data=data,
            source=result.source,
            paging=result.paging,
            warnings=[self.warning],
            label=result.label,
            save=result.save,
        )


class ShortNameEcho(ShortNameWarning):
    def __init__(self):
        super().__init__("on is offline")


def test_short_raw_name_does_not_match_inside_a_word(tmp_path):
    """control: a raw name "on" is not found inside the author's "online"."""
    manifest = fresh_manifest()
    run = session_for(ShortNameWarning, manifest, tmp_path / "word").run_case(
        "snapshot-typical"
    )
    assert run.raw_results[0].data["sensors"][0]["name"] == "on"
    assert "online" in run.outcome.envelope["warnings"][0]
    assert checks.check_warnings_params(run, set(), planted(manifest)).problems == []

    echo = session_for(ShortNameEcho, manifest, tmp_path / "echo").run_case(
        "snapshot-typical"
    )
    assert checks.check_warnings_params(echo, set(), planted(manifest)).problems != []


def test_no_get_request_is_caught_by_http_check(tmp_path):
    """a module that recorded no GET (probes cannot run) -> the http check."""
    manifest = fresh_manifest()
    session = session_for(ConformanceDemo, manifest, tmp_path)
    login_only = session.run_case("status-login-401")
    assert all(request.method != "GET" for request in login_only.requests)
    problems = checks.check_http(ConformanceDemo, session, [login_only]).problems
    assert any("no GET request" in problem for problem in problems)

    typical = session.run_case("snapshot-typical")
    assert checks.check_http(ConformanceDemo, session, [typical]).problems == []


class TagsModule(ConformanceDemo):
    """A demo variant with a declared list of untrusted strings."""

    commands = tuple(
        sdk.Command(
            command.name,
            handler=command.handler,
            description=command.description,
            required=command.required,
            optional=command.optional,
            timeout_ms=command.timeout_ms,
            fields=command.fields
            + ((sdk.Field("data.tags[]", "string", untrusted=True),)
               if command.name == "snapshot" else ()),
            schema_version=command.schema_version,
        )
        for command in ConformanceDemo.commands
    )

    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        data = json.loads(json.dumps(dict(result.data)))
        data["tags"] = ["Room ‮ sneaky", "clean"]
        return sdk.Result(data=data, source=result.source, label=result.label)


def test_dirty_string_list_counts_as_hostile_input(tmp_path):
    """control: a dirty item in a declared x[] string list counts as hostile input."""
    manifest = fresh_manifest()
    run = session_for(TagsModule, manifest, tmp_path).run_case("snapshot-typical")
    assert run.outcome.envelope["data"]["tags"] == ["Room sneaky", "clean"]
    result = checks.check_hostile_names(run)
    assert result.problems == []
    assert result.examined > 0


class EvilGet(ConformanceDemo):
    """A follow-up handler that corrupts paged data while the first page stays clean."""

    def get(self, ctx, params):
        with ctx.http("http://other-example:8443") as evil:
            evil.get("/api/v1/sensors-example")
        result = super().get(ctx, params)
        data = copy.deepcopy(result.data)
        for item in data["sensors"]:
            item["name"] = LEAK
            item["temperature_c"] = -999
            item["undeclared"] = "untrusted\x00text"
        return replace(
            result,
            data=data,
            warnings=["untrusted device 2001:db8::7 " + LEAK],
            label=LEAK,
        )


def evil_entry(cls, manifest, tmp_path):
    """A conftest-style entry for one evil module class.

    Per-case sessions, first runs, and follow-ups, exactly as the
    suite's ``all_runs`` fixture builds them. Tests below enumerate
    pages only through ``harness.page_runs`` -- the same code path
    ``test_conformance.py`` uses -- so reverting the check-every-page
    wiring to first-page-only blinds every one of them.
    """
    sessions = {}
    runs = {}
    follows = {}
    for case in manifest["cases"]:
        session = harness.CaseSession(cls, manifest, tmp_path / case["name"], FIXTURE_DIR)
        sessions[case["name"]] = session
        first = session.run_case(case["name"])
        runs[case["name"]] = first
        if isinstance(first.outcome.envelope.get("more"), dict):
            follows[case["name"]] = session.follow_paging(first)
    assert follows.get("snapshot-large"), "the evil needs follow-up pages to ride"
    return {
        "cls": cls,
        "manifest": manifest,
        "fixture_dir": FIXTURE_DIR,
        "sessions": sessions,
        "session": sessions[manifest["cases"][0]["name"]],
        "runs": runs,
        "follows": follows,
    }


def test_evil_followup_is_caught_by_every_check(tmp_path):
    """a follow-up-only evil while the first page is clean -> every per-page check."""
    manifest = fresh_manifest()
    entry = evil_entry(EvilGet, manifest, tmp_path / "bad")
    session = entry["session"]
    pages = harness.page_runs(entry, "snapshot-large")
    bad_first = pages[0]
    bad_follows = pages[1:]
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-large")
    leaves = harness.case_string_leaves(case, FIXTURE_DIR)
    found = planted(manifest)
    # The first page is clean: the evil rides only in the follow-ups.
    assert checks.check_secrets(bad_first, found).problems == []
    assert checks.check_envelope(bad_first).problems == []
    assert checks.check_fields(bad_first).problems == []
    # Each check catches its own part of the follow-up evil. The
    # envelope, stdio, and hostile-names checks pass on every page: the
    # evil pages are well-formed, silent, and sanitised; their rule
    # violations live in HTTP, secrets, fields, warnings, and paging.
    for run in pages:
        assert checks.check_envelope(run).problems == []
        assert checks.check_stdio(run).problems == []
        assert checks.check_hostile_names(run, require_dirty=False).problems == []
    assert checks.check_http(EvilGet, session, pages).problems != []
    assert any(checks.check_secrets(run, found).problems for run in pages)
    assert any(checks.check_fields(run).problems for run in pages)
    assert any(
        checks.check_warnings_params(run, leaves, found).problems for run in pages
    )
    assert (
        checks.check_size_paging(bad_first, bad_follows, session.registry).problems
        != []
    )

    good_entry = evil_entry(ConformanceDemo, manifest, tmp_path / "good")
    good_session = good_entry["session"]
    good_pages = harness.page_runs(good_entry, "snapshot-large")
    good_first, good_follows = good_pages[0], good_pages[1:]
    assert all(checks.check_envelope(run).problems == [] for run in good_pages)
    assert checks.check_http(ConformanceDemo, good_session, good_pages).problems == []
    assert all(checks.check_secrets(run, found).problems == [] for run in good_pages)
    assert all(checks.check_fields(run).problems == [] for run in good_pages)
    assert all(
        checks.check_warnings_params(run, leaves, found).problems == []
        for run in good_pages
    )
    assert (
        checks.check_size_paging(good_first, good_follows, good_session.registry)
        .problems
        == []
    )


class EvilFollowupError(ConformanceDemo):
    """A follow-up handler that fails while the first page stays clean."""

    def get(self, ctx, params):
        if params.get("offset", 0) > 0:
            raise sdk.CommandError("upstream_unexpected", "Cannot page.")
        return super().get(ctx, params)


def test_evil_followup_error_is_caught_by_envelope_check(tmp_path):
    """a follow-up-only error envelope -> the envelope check over every page."""
    manifest = fresh_manifest()
    entry = evil_entry(EvilFollowupError, manifest, tmp_path / "bad")
    pages = harness.page_runs(entry, "snapshot-large")
    assert len(pages) == 2
    assert checks.check_envelope(pages[0]).problems == []
    assert any(checks.check_envelope(run).problems != [] for run in pages)

    good_entry = evil_entry(ConformanceDemo, manifest, tmp_path / "good")
    good_pages = harness.page_runs(good_entry, "snapshot-large")
    assert all(checks.check_envelope(run).problems == [] for run in good_pages)


class EvilFollowupPrint(ConformanceDemo):
    """A follow-up handler that prints while the first page stays silent."""

    def get(self, ctx, params):
        if params.get("offset", 0) > 0:
            print(LEAK)
        return super().get(ctx, params)


def test_evil_followup_print_is_caught_by_stdio_check(tmp_path):
    """a follow-up-only stray print -> the stdio check over every page."""
    manifest = fresh_manifest()
    entry = evil_entry(EvilFollowupPrint, manifest, tmp_path / "bad")
    pages = harness.page_runs(entry, "snapshot-large")
    assert checks.check_stdio(pages[0]).problems == []
    assert any(checks.check_stdio(run).problems != [] for run in pages)
    assert any(
        checks.check_secrets(run, planted(manifest)).problems != [] for run in pages
    )

    good_entry = evil_entry(ConformanceDemo, manifest, tmp_path / "good")
    good_pages = harness.page_runs(good_entry, "snapshot-large")
    assert all(checks.check_stdio(run).problems == [] for run in good_pages)


class EvilFollowupDirty(ConformanceDemo):
    """A follow-up handler returning dirty names while the first page stays clean."""

    def get(self, ctx, params):
        result = super().get(ctx, params)
        if params.get("offset", 0) > 0:
            data = copy.deepcopy(result.data)
            for item in data["sensors"]:
                item["name"] = "Room \x00 sneaky"
            return replace(result, data=data)
        return result


def test_evil_followup_dirty_is_caught_by_hostile_check(tmp_path, monkeypatch):
    """a follow-up-only dirty name with the sanitiser off -> the hostile check."""
    manifest = fresh_manifest()
    monkeypatch.setattr(runner, "sanitize_mapping", lambda data, fields: data)
    entry = evil_entry(EvilFollowupDirty, manifest, tmp_path / "bad")
    pages = harness.page_runs(entry, "snapshot-large")
    # The first page is clean even with the sanitiser off: typical names
    # hold nothing to sanitise, so only a follow-up page can fail here.
    assert checks.check_hostile_names(pages[0], require_dirty=False).problems == []
    assert any(
        checks.check_hostile_names(run, require_dirty=False).problems != []
        for run in pages
    )

    monkeypatch.undo()
    good_entry = evil_entry(ConformanceDemo, manifest, tmp_path / "good")
    good_pages = harness.page_runs(good_entry, "snapshot-large")
    assert all(
        checks.check_hostile_names(run, require_dirty=False).problems == []
        for run in good_pages
    )


# References saved at import time: a handler writing through one bypasses
# the sys.stdout/sys.stderr swap, so only the fd-level capture sees it.
_SAVED_OUT = sys.__stdout__
_SAVED_ERR = sys.__stderr__


class PrintLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        print(LEAK)
        print("x" * 100000)
        print(LEAK, file=sys.stderr)
        return super().snapshot(ctx, params)


def test_stray_stdout_and_stderr_are_caught(tmp_path):
    """a handler writing outside the runner log stream -> the stdio check."""
    manifest = fresh_manifest()
    bad = session_for(PrintLeak, manifest, tmp_path / "bad").run_case("snapshot-typical")
    assert LEAK in bad.stdout_captured
    assert LEAK in bad.stderr_captured
    assert checks.check_stdio(bad).problems != []
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert good.stdout_captured == ""
    assert good.stderr_captured == ""
    assert checks.check_stdio(good).problems == []
    assert checks.check_secrets(good, planted(manifest)).problems == []


class FdWriteLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        os.write(1, (LEAK + "\n").encode())
        os.write(2, (LEAK + "\n").encode())
        return super().snapshot(ctx, params)


def test_os_write_to_fds_is_caught(tmp_path):
    """os.write(1/2, ...) bypasses the sys swap -> the stdio check (fd capture)."""
    manifest = fresh_manifest()
    bad = session_for(FdWriteLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert LEAK.encode() in bad.fd_stdout_captured
    assert LEAK.encode() in bad.fd_stderr_captured
    assert bad.stdout_captured == ""
    assert bad.stderr_captured == ""
    assert checks.check_stdio(bad).problems != []
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert good.fd_stdout_captured == b""
    assert good.fd_stderr_captured == b""
    assert checks.check_stdio(good).problems == []
    assert checks.check_secrets(good, planted(manifest)).problems == []


class DunderWriteLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        sys.__stdout__.write(LEAK + "\n")
        sys.__stdout__.flush()
        sys.__stderr__.write(LEAK + "\n")
        sys.__stderr__.flush()
        return super().snapshot(ctx, params)


def test_dunder_stdout_stderr_writes_are_caught(tmp_path):
    """sys.__stdout__/__stderr__ bypass the sys swap -> the stdio check (fd capture)."""
    manifest = fresh_manifest()
    bad = session_for(DunderWriteLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert LEAK.encode() in bad.fd_stdout_captured
    assert LEAK.encode() in bad.fd_stderr_captured
    assert checks.check_stdio(bad).problems != []
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_stdio(good).problems == []
    assert checks.check_secrets(good, planted(manifest)).problems == []


class SavedRefWriteLeak(ConformanceDemo):
    def snapshot(self, ctx, params):
        _SAVED_OUT.write(LEAK + "\n")
        _SAVED_OUT.flush()
        _SAVED_ERR.write(LEAK + "\n")
        _SAVED_ERR.flush()
        return super().snapshot(ctx, params)


def test_saved_stdout_reference_write_is_caught(tmp_path):
    """a stdout/stderr object saved at import time -> the stdio check (fd capture)."""
    manifest = fresh_manifest()
    bad = session_for(SavedRefWriteLeak, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert LEAK.encode() in bad.fd_stdout_captured
    assert LEAK.encode() in bad.fd_stderr_captured
    assert checks.check_stdio(bad).problems != []
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_stdio(good).problems == []
    assert checks.check_secrets(good, planted(manifest)).problems == []


class DebugExcInfo(ConformanceDemo):
    def snapshot(self, ctx, params):
        try:
            raise ValueError(LEAK)
        except ValueError:
            ctx.log.debug("failed to read note", exc_info=True)
        return super().snapshot(ctx, params)


def test_debug_exc_info_leak_is_caught(tmp_path):
    """a PLANTED-SECRET in a DEBUG exc_info traceback -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(DebugExcInfo, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert any(LEAK in record for record in bad.log_records)
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class InfoExcInfo(ConformanceDemo):
    def snapshot(self, ctx, params):
        try:
            raise ValueError(LEAK)
        except ValueError:
            ctx.log.info("failed to read note", exc_info=True)
        return super().snapshot(ctx, params)


def test_info_exc_info_leak_is_caught(tmp_path):
    """a PLANTED-SECRET in an INFO exc_info traceback -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(InfoExcInfo, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert any(LEAK in record for record in bad.log_records)
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class DebugStackInfo(ConformanceDemo):
    def snapshot(self, ctx, params):
        ctx.log.debug("noting state", stack_info=True)  # PLANTED-SECRET-typical-1
        return super().snapshot(ctx, params)


def test_debug_stack_info_leak_is_caught(tmp_path):
    """a PLANTED-SECRET in DEBUG stack_info source text -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(DebugStackInfo, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert any(LEAK in record for record in bad.log_records)
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class InfoStackInfo(ConformanceDemo):
    def snapshot(self, ctx, params):
        ctx.log.info("noting state", stack_info=True)  # PLANTED-SECRET-typical-1
        return super().snapshot(ctx, params)


def test_info_stack_info_leak_is_caught(tmp_path):
    """a PLANTED-SECRET in INFO stack_info source text -> the planted-secrets check."""
    manifest = fresh_manifest()
    bad = session_for(InfoStackInfo, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert any(LEAK in record for record in bad.log_records)
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, planted(manifest)).problems == []


class TupleData(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        data = copy.deepcopy(result.data)
        for item in data["sensors"]:
            item["undeclared"] = "extra"
        data["sensors"] = tuple(data["sensors"])
        return replace(result, data=data, compact=data)


def test_tuple_data_hides_undeclared_key(tmp_path):
    """a tuple of mappings hides an undeclared data key -> the declared-fields check."""
    manifest = fresh_manifest()
    bad = session_for(TupleData, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert isinstance(bad.raw_results[0].data["sensors"], tuple)
    assert checks.check_fields(bad).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_fields(good).problems == []


class TupleCompact(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        compact = copy.deepcopy(result.compact)
        for item in compact["sensors"]:
            item["undeclared"] = "extra"
        compact["sensors"] = tuple(compact["sensors"])
        return replace(result, compact=compact)


def test_tuple_compact_hides_undeclared_key(tmp_path):
    """a tuple of mappings hides an undeclared compact key -> the declared-fields check."""
    manifest = fresh_manifest()
    bad = session_for(TupleCompact, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert isinstance(bad.raw_results[0].compact["sensors"], tuple)
    assert checks.check_fields(bad).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_fields(good).problems == []


class TupleSourceModule(ConformanceDemo):
    commands = tuple(
        sdk.Command(
            command.name,
            handler=command.handler,
            description=command.description,
            required=command.required,
            optional=command.optional,
            timeout_ms=command.timeout_ms,
            fields=command.fields
            + (
                (sdk.Field("source.servers[].host", "string"),)
                if command.name == "snapshot"
                else ()
            ),
            schema_version=command.schema_version,
        )
        for command in ConformanceDemo.commands
    )

    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        source = copy.deepcopy(result.source)
        source["servers"] = ({"host": "h", "sneaky": 1},)
        return replace(result, source=source)


def test_tuple_source_hides_undeclared_key(tmp_path):
    """a tuple of mappings hides an undeclared source key -> the declared-fields check."""
    manifest = fresh_manifest()
    bad = session_for(TupleSourceModule, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert isinstance(bad.raw_results[0].source["servers"], tuple)
    assert checks.check_fields(bad).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_fields(good).problems == []


class InlineLabel(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        return replace(result, label="PLANTED-SECRET-inline-9")


def inline_manifest():
    """A manifest whose typical route is an inline JSON body plus secret headers."""
    manifest = fresh_manifest()
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-typical")
    payload = json.loads((FIXTURE_DIR / "sensors_typical.json").read_text(encoding="utf-8"))
    payload["unused_secret"] = "PLANTED-SECRET-inline-9"
    case["routes"][0].pop("body_file")
    case["routes"][0]["body"] = json.dumps(payload)
    case["routes"][0]["headers"] = {"X-Note": "PLANTED-SECRET-inline-10"}
    return manifest


def test_inline_json_body_and_headers_are_harvested(tmp_path):
    """an inline body parses as JSON for leaf harvest, headers too -> the secrets check."""
    manifest = inline_manifest()
    found = harness.planted_strings(manifest, FIXTURE_DIR)
    assert "PLANTED-SECRET-inline-9" in found
    assert "PLANTED-SECRET-inline-10" in found
    leaves = harness.upstream_string_leaves(manifest, FIXTURE_DIR)
    assert "PLANTED-SECRET-inline-10" in leaves
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-typical")
    assert "PLANTED-SECRET-inline-10" in harness.case_string_leaves(case, FIXTURE_DIR)
    bad = session_for(InlineLabel, manifest, tmp_path / "bad").run_case(
        "snapshot-typical"
    )
    assert bad.outcome.envelope["label"] == "PLANTED-SECRET-inline-9"
    assert checks.check_secrets(bad, found).problems != []

    good = session_for(ConformanceDemo, manifest, tmp_path / "good").run_case(
        "snapshot-typical"
    )
    assert checks.check_secrets(good, found).problems == []


def paging_first_and_follows(cls, manifest, tmp_path):
    """A truncated first page plus its follow-ups for one module class."""
    session = session_for(cls, manifest, tmp_path)
    first = session.run_case("snapshot-large")
    assert first.outcome.envelope.get("truncated") is True
    return session, first, session.follow_paging(first)


def test_lying_offset_is_caught_by_paging_check(tmp_path):
    """more.params offset must equal start + items kept -> the size/paging check."""
    manifest = fresh_manifest()
    session, first, follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "bad"
    )
    bad = copy.copy(first)
    bad.outcome = copy.copy(first.outcome)
    bad.outcome.envelope = copy.deepcopy(first.outcome.envelope)
    bad.outcome.envelope["more"]["params"]["offset"] = 123456
    assert checks.check_size_paging(bad, follows, session.registry).problems != []

    good_session, good_first, good_follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "good"
    )
    assert (
        checks.check_size_paging(good_first, good_follows, good_session.registry)
        .problems
        == []
    )


def test_lying_remaining_is_caught_by_paging_check(tmp_path):
    """more.remaining must equal the true count left -> the size/paging check."""
    manifest = fresh_manifest()
    session, first, follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "bad"
    )
    bad = copy.copy(first)
    bad.outcome = copy.copy(first.outcome)
    bad.outcome.envelope = copy.deepcopy(first.outcome.envelope)
    bad.outcome.envelope["more"]["remaining"] = {"sensors": 999999}
    assert checks.check_size_paging(bad, follows, session.registry).problems != []

    good_session, good_first, good_follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "good"
    )
    assert (
        checks.check_size_paging(good_first, good_follows, good_session.registry)
        .problems
        == []
    )


def test_non_terminating_chain_is_caught_by_paging_check(tmp_path):
    """the last page must have more null, not loop -> the size/paging check."""
    manifest = fresh_manifest()
    session, first, follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "bad"
    )
    loop = copy.copy(follows[-1])
    loop.outcome = copy.copy(follows[-1].outcome)
    loop.outcome.envelope = copy.deepcopy(follows[-1].outcome.envelope)
    loop.outcome.envelope["truncated"] = True
    loop.outcome.envelope["more"] = copy.deepcopy(first.outcome.envelope["more"])
    result = checks.check_size_paging(first, follows[:-1] + [loop], session.registry)
    assert result.problems != []
    assert any("did not terminate" in problem for problem in result.problems)

    good_session, good_first, good_follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "good"
    )
    assert (
        checks.check_size_paging(good_first, good_follows, good_session.registry)
        .problems
        == []
    )


def scaled_large_manifest(factor=4):
    """A manifest whose large case serves ``factor`` times the sensors."""
    manifest = fresh_manifest()
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-large")
    payload = json.loads(
        (FIXTURE_DIR / case["routes"][0].pop("body_file")).read_text(encoding="utf-8")
    )
    payload["sensors"] *= factor
    case["routes"][0]["body"] = json.dumps(payload)
    return manifest


def test_paging_with_identifiers_none_passes(tmp_path):
    """identifiers:none projects ids out of every page -> the size/paging check accepts."""
    manifest = scaled_large_manifest()
    session = harness.CaseSession(
        ConformanceDemo, manifest, tmp_path / "none", FIXTURE_DIR,
        identifiers="none",
    )
    first = session.run_case("snapshot-large")
    assert first.outcome.envelope.get("truncated") is True
    follows = session.follow_paging(first)
    assert follows, "need follow-up pages to exercise the projection"
    assert first.saved_obj is not None
    assert any("id" in item for item in first.saved_obj["data"]["sensors"])
    for run in [first] + follows:
        for item in run.outcome.envelope["data"]["sensors"]:
            assert "id" not in item
    result = checks.check_size_paging(first, follows, session.registry)
    assert result.problems == []
    assert result.examined > 0


class CompactWithoutId(ConformanceDemo):
    def snapshot(self, ctx, params):
        result = super().snapshot(ctx, params)
        compact = copy.deepcopy(result.compact)
        for item in compact["sensors"]:
            item.pop("id")
        return replace(result, compact=compact)


def test_compact_without_id_paging_passes(tmp_path):
    """a compact form that drops the item id -> the size/paging check still accepts."""
    # Scaled so the id-less compact still exceeds the budget and pages.
    manifest = scaled_large_manifest()
    session = session_for(CompactWithoutId, manifest, tmp_path)
    first = session.run_case("snapshot-large")
    assert first.outcome.envelope.get("truncated") is True
    follows = session.follow_paging(first)
    assert follows, "need follow-up pages to exercise the projection"
    result = checks.check_size_paging(first, follows, session.registry)
    assert result.problems == []
    assert result.examined > 0


def test_changed_values_are_caught_by_paging_check(tmp_path):
    """returned items must match the saved items field by field -> the size/paging check."""
    manifest = fresh_manifest()
    session, first, follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "bad"
    )
    fakes = []
    for run in follows:
        fake = copy.copy(run)
        fake.outcome = copy.copy(run.outcome)
        fake.outcome.envelope = copy.deepcopy(run.outcome.envelope)
        fakes.append(fake)
    for item in fakes[0].outcome.envelope["data"]["sensors"]:
        item["temperature_c"] = 999
    result = checks.check_size_paging(first, fakes, session.registry)
    assert result.problems != []
    assert any("differs from the saved" in problem for problem in result.problems)

    good_session, good_first, good_follows = paging_first_and_follows(
        ConformanceDemo, manifest, tmp_path / "good"
    )
    assert (
        checks.check_size_paging(good_first, good_follows, good_session.registry)
        .problems
        == []
    )


class PrunedSecret(ConformanceDemo):
    def snapshot(self, ctx, params):
        ctx.store.save("snapshot", {"leak": LEAK})
        ctx.store.save("snapshot", {"clean": True})
        return super().snapshot(ctx, params)


def test_pruned_saved_payload_is_still_scanned(tmp_path, monkeypatch):
    """saves are recorded at write time, before retention prunes -> the secrets check."""
    from datetime import datetime, timedelta, timezone

    from housesitter.core import store as store_module

    manifest = fresh_manifest()
    # Same-second saves sort by random suffix, so force pruning order
    # with a ticking clock: every save lands in its own later second,
    # and the planted first save is always the one retention prunes.
    ticks = iter(range(1000000))
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        store_module.FileStore,
        "_timestamp",
        lambda self: (base + timedelta(seconds=next(ticks))).strftime(
            "%Y%m%dT%H%M%SZ"
        ),
    )
    session = harness.CaseSession(
        PrunedSecret, manifest, tmp_path / "bad", FIXTURE_DIR, retention=1
    )
    bad = session.run_case("snapshot-typical")
    survivors = [path for path in bad.data_dir.rglob("*") if path.is_file()]
    assert survivors, "retention=1 must leave one save behind"
    assert any(LEAK in payload for payload in bad.saved_history)
    for path in survivors:
        assert LEAK not in path.read_text(encoding="utf-8"), (
            "the planted save must be pruned from disk before scanning, "
            "so only the write-time recording can catch it"
        )
    assert checks.check_secrets(bad, planted(manifest)).problems != []

    good_session = harness.CaseSession(
        ConformanceDemo, manifest, tmp_path / "good", FIXTURE_DIR, retention=1
    )
    good = good_session.run_case("snapshot-typical")
    assert checks.check_secrets(good, planted(manifest)).problems == []


def test_bracket_front_matter_is_caught(tmp_path):
    """name: [ is not a plain scalar -> the skill check."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    bad_text = text.replace(
        "name: housesitter-conformance-demo", "name: [", 1
    )
    assert "name: [" in bad_text
    bad = skill_module(tmp_path, bad_text, "pkg_bracket")
    assert checks.check_skill(bad).problems != []

    assert checks.check_skill(ConformanceDemo).problems == []


def test_comment_front_matter_is_caught(tmp_path):
    """name: # none is blank after stripping comments -> the skill check."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    bad_text = text.replace(
        "name: housesitter-conformance-demo", "name: # none", 1
    )
    assert "name: # none" in bad_text
    bad = skill_module(tmp_path, bad_text, "pkg_comment")
    assert checks.check_skill(bad).problems != []

    assert checks.check_skill(ConformanceDemo).problems == []


def skill_with_name(tmp_path, name_value, modname):
    """The demo SKILL.md with its name line replaced by ``name_value``."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    renamed = text.replace(
        "name: housesitter-conformance-demo", f"name: {name_value}", 1
    )
    assert renamed != text
    return skill_module(tmp_path, renamed, modname)


def test_nested_colon_name_is_caught(tmp_path):
    """name: valid: nested holds an unquoted colon-space -> the skill check."""
    bad = skill_with_name(tmp_path, "valid: nested", "pkg_nested")
    problems = checks.check_skill(bad).problems
    assert any("unquoted" in problem for problem in problems)

    assert checks.check_skill(ConformanceDemo).problems == []


@pytest.mark.parametrize(
    "slug,word",
    [
        ("null", "null"),
        ("tilde", "~"),
        ("true", "true"),
        ("false", "False"),
        ("yes", "yes"),
        ("no", "NO"),
    ],
)
def test_null_like_name_is_caught(tmp_path, slug, word):
    """a YAML null/bool word as name is not a name -> the skill check."""
    bad = skill_with_name(tmp_path, word, f"pkg_nulllike_{slug}")
    assert checks.check_skill(bad).problems != []

    assert checks.check_skill(ConformanceDemo).problems == []


@pytest.mark.parametrize(
    "slug,scalar",
    [
        ("double", '"[literal]"'),
        ("single", "'[literal]'"),
        ("quotednull", '"null"'),
    ],
)
def test_quoted_literal_name_passes(tmp_path, slug, scalar):
    """control: quoted text is its own scalar, even "[literal]" or "null"."""
    good = skill_with_name(tmp_path, scalar, f"pkg_quoted_{slug}")
    assert checks.check_skill(good).problems == []


def test_unclosed_quote_name_is_caught(tmp_path):
    """name: "[literal] never closes its quote -> the skill check."""
    bad = skill_with_name(tmp_path, '"[literal]', "pkg_unclosed")
    problems = checks.check_skill(bad).problems
    assert any("quote never closes" in problem for problem in problems)

    assert checks.check_skill(ConformanceDemo).problems == []


def test_block_scalar_description_passes(tmp_path):
    """control: description: >- with content is the documented working form."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    assert "description: >-" in text
    assert checks.check_skill(ConformanceDemo).problems == []
    empty = text.replace(
        "description: >-\n"
        "  Read temperature and humidity sensors (ids, names, readings,\n"
        "  reachability) from the test-only demo sensor hub through Housesitter on\n"
        "  this Pi. Read-only.\n",
        "description: >-\n",
        1,
    )
    assert empty != text
    bad = skill_module(tmp_path, empty, "pkg_emptyblock")
    assert checks.check_skill(bad).problems != []


def test_skill_text_with_secret_is_caught(tmp_path):
    """SKILL.md text itself can carry a planted secret -> the skill check."""
    text = SKILL_PATH.read_text(encoding="utf-8")
    bad_text = text.replace("Read-only.", "Read-only. " + LEAK, 1)
    assert LEAK in bad_text
    bad = skill_module(tmp_path, bad_text, "pkg_leakskill")
    assert checks.check_skill(bad, {LEAK}).problems != []

    assert checks.check_skill(ConformanceDemo, {LEAK}).problems == []


class StaticSnapshot(ConformanceDemo):
    @staticmethod
    def snapshot(ctx, params):
        return sdk.Result(
            data={"sensor_count": 7},
            source={"kind": "sensor_hub", "api_version": None},
        )


class ClassSnapshot(ConformanceDemo):
    @classmethod
    def snapshot(cls, ctx, params):
        return sdk.Result(
            data={"sensor_count": 7},
            source={"kind": "sensor_hub", "api_version": None},
        )


def test_static_class_and_instance_handlers_all_record(tmp_path):
    """recording preserves static/class/instance binding -> the harness runs all three."""
    manifest = fresh_manifest()
    for cls, label in (
        (StaticSnapshot, "static"),
        (ClassSnapshot, "class"),
        (ConformanceDemo, "instance"),
    ):
        run = session_for(cls, manifest, tmp_path / label).run_case("snapshot-typical")
        assert run.outcome.exit_code == 0, label
        assert run.raw_results, label


class QueryDemo(ConformanceDemo):
    http_allow = tuple(
        sdk.Allow(entry.method, entry.pattern, allow_query=True)
        if entry.method == "GET"
        else entry
        for entry in ConformanceDemo.http_allow
    )

    def snapshot(self, ctx, params):
        with ctx.http(ctx.config["base_url"]) as live:
            live.get("/api/v1/sensors-example?q=1")
        return super().snapshot(ctx, params)


def query_manifest():
    """A manifest whose typical route also serves the recorded query target."""
    manifest = fresh_manifest()
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-typical")
    case["routes"].append({**case["routes"][0], "path": "/api/v1/sensors-example?q=1"})
    return manifest


def test_query_bearing_target_probes_from_path(tmp_path):
    """a recorded ?q=1 with allow_query passes probes; unopted ?q stays refused."""
    from core.helpers import ScriptedServer, canned_handler
    from housesitter.core import http as http_core

    manifest = query_manifest()
    session = session_for(QueryDemo, manifest, tmp_path / "bad")
    run = session.run_case("snapshot-typical")
    assert run.outcome.exit_code == 0
    assert checks.check_http(QueryDemo, session, [run]).problems == []

    # The plain demo never opts into queries: the same target is refused.
    good_session = session_for(ConformanceDemo, manifest, tmp_path / "good")
    server = ScriptedServer(canned_handler({}))
    try:
        ctx = good_session.context_for(server.port)
        base = f"http://127.0.0.1:{server.port}"
        with ctx.http(base) as live:
            try:
                live.get("/api/v1/sensors-example?q=1")
            except http_core.NotAllowed:
                pass
            else:
                raise AssertionError("an unopted ?q=1 was not refused")
    finally:
        server.close()

    good = good_session.run_case("snapshot-typical")
    assert checks.check_http(ConformanceDemo, good_session, [good]).problems == []


_OPAQUE_ONLY_FIELDS = (
    sdk.Field("data.blob", "array"),
    sdk.Field("data.sensor_count", "integer"),
    sdk.Field("source.kind", "string"),
    sdk.Field("source.api_version", "string", untrusted=True, max_len=32),
)


class OpaqueOnlyModule(ConformanceDemo):
    """A demo variant whose only list-like data field is opaque.

    No ``x[]`` paths anywhere under ``data`` on any command: only
    ``Field("data.blob", "array")`` marks the output as unbounded, so
    removing opaque-array detection from the truncation-list guard must
    blind it to this module. The variant is never executed; only its
    declarations are inspected.
    """

    commands = tuple(
        sdk.Command(
            command.name,
            handler=command.handler,
            description=command.description,
            required=command.required,
            optional=command.optional,
            timeout_ms=command.timeout_ms,
            fields=_OPAQUE_ONLY_FIELDS,
            schema_version=command.schema_version,
        )
        for command in ConformanceDemo.commands
    )


class ScalarOnlyModule(ConformanceDemo):
    commands = (
        sdk.Command(
            "snapshot",
            handler="snapshot",
            description="Read one number.",
            fields=(sdk.Field("data.sensor_count", "integer"),),
        ),
    )

    def snapshot(self, ctx, params):
        return sdk.Result(
            data={"sensor_count": 1},
            source={"kind": "sensor_hub", "api_version": None},
        )


def test_opaque_array_counts_as_list_for_truncation():
    """Field(type=array) under data is a list -> the truncation-list guard."""
    data_paths = [
        item.path
        for command in OpaqueOnlyModule.commands
        for item in command.fields
        if item.path.startswith("data.")
    ]
    assert data_paths, "the variant must declare data fields"
    assert not any("[]" in path for path in data_paths)
    assert checks.module_has_lists(OpaqueOnlyModule)
    assert checks.module_has_lists(ConformanceDemo)
    assert not checks.module_has_lists(ScalarOnlyModule)


class EnvSetter(ConformanceDemo):
    def snapshot(self, ctx, params):
        os.environ["ASTRA_CONFORMANCE_SENTINEL"] = "set-in-case"
        return super().snapshot(ctx, params)


def test_env_does_not_leak_between_runs(tmp_path):
    """os.environ is restored after each run -> case isolation."""
    manifest = fresh_manifest()
    session = session_for(EnvSetter, manifest, tmp_path)
    os.environ.pop("ASTRA_CONFORMANCE_SENTINEL", None)
    session.run_case("snapshot-typical")
    assert os.environ.get("ASTRA_CONFORMANCE_SENTINEL") is None
    second = session.run_case("status-typical")
    assert second.outcome.envelope.get("label") != "set-in-case"
    assert os.environ.get("ASTRA_CONFORMANCE_SENTINEL") is None


def test_fenced_spec_tables_are_ignored(tmp_path):
    """tables inside fenced code blocks are not spec tables -> the spec check."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    target = tmp_path / "SPEC.md"
    target.write_text("```text\n" + text + "\n```\n", encoding="utf-8")
    assert checks.check_spec(ConformanceDemo, target).problems != []

    assert checks.check_spec(ConformanceDemo, SPEC_PATH).problems == []


def test_duplicated_spec_table_is_caught(tmp_path):
    """one table per object: a duplicated object table -> the spec check."""
    text = SPEC_PATH.read_text(encoding="utf-8")
    target = tmp_path / "SPEC.md"
    target.write_text(text + "\n" + text, encoding="utf-8")
    assert checks.check_spec(ConformanceDemo, target).problems != []

    assert checks.check_spec(ConformanceDemo, SPEC_PATH).problems == []


def test_nonempty_data_under_empty_label_is_caught(tmp_path):
    """an empty-fixture case that expects ok returns only empty lists -> the envelope check."""
    manifest = fresh_manifest()
    case = next(item for item in manifest["cases"] if item["name"] == "snapshot-empty")
    typical = next(
        item for item in manifest["cases"] if item["name"] == "snapshot-typical"
    )
    case["routes"] = copy.deepcopy(typical["routes"])
    bad = session_for(ConformanceDemo, manifest, tmp_path / "bad").run_case(
        "snapshot-empty"
    )
    assert bad.outcome.exit_code == 0
    assert bad.outcome.envelope["data"]["sensors"] != []
    assert checks.check_envelope(bad).problems != []

    good = session_for(ConformanceDemo, fresh_manifest(), tmp_path / "good").run_case(
        "snapshot-empty"
    )
    assert checks.check_envelope(good).problems == []


@pytest.mark.parametrize(
    "name,source",
    [
        ("builtins_import", "import builtins\n"),
        ("builtins_from", "from builtins import str\n"),
        ("dunder_builtins", "x = __builtins__\n"),
        ("sys_modules", "import sys\nx = sys.modules\n"),
        ("builtins_import_attr", "import builtins\nbuiltins.__import__('os')\n"),
        ("os_system", "import os\nos.system('true')\n"),
        ("os_popen", "import os\nos.popen('true').read()\n"),
        ("os_spawn", "import os\nos.spawnl(os.P_WAIT, 'true')\n"),
        ("os_exec", "import os\nos.execv('true', [])\n"),
        ("os_fork", "import os\nos.fork()\n"),
        ("os_posix_spawn", "import os\nos.posix_spawn('true', [], None, [])\n"),
        ("pty_import", "import pty\n"),
        ("multiprocessing_import", "import multiprocessing\n"),
        ("multiprocessing_from", "from multiprocessing import connection\n"),
        ("futures_process", "import concurrent.futures.process\n"),
        ("futures_process_from", "from concurrent.futures.process import Process\n"),
        ("asyncio_exec", "import asyncio\nasyncio.create_subprocess_exec('true')\n"),
        ("asyncio_shell", "import asyncio\nasyncio.create_subprocess_shell('true')\n"),
        ("bare_open", "open('/tmp/x', 'w').write('v')\n"),
        ("io_open", "import io\nio.open('/tmp/x', 'w').write('v')\n"),
    ],
)
def test_banned_forms_are_caught_by_import_lint(tmp_path, name, source):
    """each indirect route past the import scan -> the import lint."""
    bad = write_source(tmp_path, name + ".py", source)
    assert scan_module_file(bad, "demo_module") != [], source

    good = write_source(tmp_path, "good.py", "import json\n")
    assert scan_module_file(good, "demo_module") == []
