"""Pin the invoke pipeline: params, context, handlers, policy, budget."""

from __future__ import annotations

import io
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from housesitter import __version__, sdk
from housesitter.core import runner
from housesitter.core.config import Config, ModuleConfig
from housesitter.core.http import NotAllowed, UpstreamError
from housesitter.core.registry import Registry
from housesitter.core.store import StoreError

FIXED_NOW = datetime(2026, 11, 4, 15, 15, 0, tzinfo=timezone.utc)

PROBE_COMMAND = sdk.Command(
    name="probe",
    handler="probe",
    description="Probe the demo sensor.",
    required={"count": sdk.Param("integer", "How many.", minimum=1, maximum=10)},
    optional={
        "label": sdk.Param("string", "Note.", max_len=10),
        "mode": sdk.Param("string", "Mode.", enum=("a", "b"), default="a"),
        "flag": sdk.Param("boolean", "Flag."),
        "ratio": sdk.Param("number", "Ratio."),
        "tags": sdk.Param("array", "Tags."),
        "opts": sdk.Param("object", "Opts."),
    },
    fields=(sdk.Field("data.echo", "string"),),
)


def echo_handler(self, ctx, params):
    return sdk.Result(data={"echo": json.dumps(params, sort_keys=True)})


def make_probe_module(handler=echo_handler, module_id="demo_sensor"):
    return type(
        f"ProbeModule_{module_id}",
        (sdk.Module,),
        {
            "id": module_id,
            "title": "Demo sensor",
            "version": "0.1.0",
            "config": {},
            "http_allow": (),
            "commands": (PROBE_COMMAND,),
            "probe": handler,
        },
    )


def make_config(tmp_path: Path, module_id: str, **overrides) -> Config:
    settings = {
        "identifiers": "full",
        "retention": 500,
        "modules": {module_id: ModuleConfig(enabled=True, retention=500, settings={})},
    }
    settings.update(overrides)
    return Config(
        path=tmp_path / "config.json",
        secrets_dir=tmp_path / "secrets",
        identifiers=settings["identifiers"],
        retention=settings["retention"],
        modules=settings["modules"],
    )


def run(
    module_cls,
    full_name,
    raw,
    tmp_path: Path,
    *,
    config=None,
    identifiers="full",
    **kwargs,
):
    registry = Registry.from_classes([module_cls])
    if config is None:
        config = make_config(tmp_path, module_cls.id, identifiers=identifiers)
    stream = io.StringIO()
    outcome = runner.run_command(
        full_name,
        raw,
        registry=registry,
        env={"HOME": str(tmp_path)},
        config=config,
        now=lambda: FIXED_NOW,
        log_stream=stream,
        **kwargs,
    )
    return outcome, stream.getvalue()


def failed(outcome, code, module="demo_sensor"):
    assert outcome.exit_code == 1
    assert outcome.envelope["ok"] is False
    assert outcome.envelope["error"]["code"] == code
    assert outcome.error_module == module
    assert outcome.error_code == code
    line = runner.format_error_line(outcome)
    assert line.startswith(f"housesitter: {module}: {code}: ")
    assert len(line) < 400
    return line


def echo_data(outcome):
    assert outcome.exit_code == 0
    return json.loads(outcome.envelope["data"]["echo"])


class TestResolve:
    def test_unknown_command_is_not_found(self, tmp_path):
        outcome, _ = run(make_probe_module(), "housesitter.nope.snapshot", None, tmp_path)
        failed(outcome, "not_found", module="housesitter")
        assert "housesitter.nope.snapshot" in outcome.error_reason

    def test_disabled_module_is_config_invalid(self, tmp_path):
        module_cls = make_probe_module()
        config = make_config(
            tmp_path,
            module_cls.id,
            modules={module_cls.id: ModuleConfig(enabled=False, retention=500)},
        )
        outcome, _ = run(
            module_cls, "housesitter.demo_sensor.probe", {"count": 1}, tmp_path,
            config=config,
        )
        line = failed(outcome, "config_invalid")
        assert "is not enabled in the config" in line

    def test_config_load_failure_names_key(self, tmp_path):
        module_cls = make_probe_module()
        bad_home = tmp_path / "badhome"
        config_dir = bad_home / ".config" / "housesitter"
        config_dir.mkdir(parents=True)
        (config_dir / "config.json").write_text("{not json")
        registry = Registry.from_classes([module_cls])
        outcome = runner.run_command(
            "housesitter.demo_sensor.probe",
            {"count": 1},
            registry=registry,
            env={"HOME": str(bad_home)},
            config=None,
            now=lambda: FIXED_NOW,
            log_stream=io.StringIO(),
        )
        line = failed(outcome, "config_invalid")
        assert "not valid JSON" in line
        assert "{" not in outcome.error_reason


class TestParams:
    @pytest.mark.parametrize(
        "raw, fragment",
        [
            ({"count": 1, "bogus": 2}, "unknown parameter 'bogus'"),
            ({}, "is a required parameter"),
            ({"count": None}, "is a required parameter"),
            ({"count": True}, "must be integer"),
            ({"count": 1.5}, "must be integer"),
            ({"count": 0}, "below the minimum"),
            ({"count": 11}, "above the maximum"),
            ({"count": 1, "mode": "z"}, "not one of"),
            ({"count": 1, "label": "x" * 11}, "longer than allowed"),
            ({"count": 1, "flag": "yes"}, "must be boolean"),
            ({"count": 1, "ratio": "x"}, "must be number"),
            ({"count": 1, "tags": "x"}, "must be array"),
            ({"count": 1, "opts": [1]}, "must be object"),
            ("[1]", "must be a JSON object"),
            ("xxx", "not valid JSON"),
            ("x" * (1024 * 1024 + 1), "too large"),
        ],
    )
    def test_bad_params(self, tmp_path, raw, fragment):
        outcome, _ = run(make_probe_module(), "housesitter.demo_sensor.probe", raw, tmp_path)
        line = failed(outcome, "bad_params")
        assert fragment in line

    def test_nan_is_not_a_number(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe",
            '{"count": 1, "ratio": NaN}', tmp_path,
        )
        failed(outcome, "bad_params")

    def test_infinite_is_not_an_integer(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe",
            '{"count": 1e400}', tmp_path,
        )
        failed(outcome, "bad_params")

    def test_null_optional_is_absent_and_defaults_fill(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe",
            {"count": 2, "label": None}, tmp_path,
        )
        assert echo_data(outcome) == {"count": 2, "mode": "a"}

    def test_empty_input_means_no_params(self, tmp_path):
        for raw in (None, b"", b"  \n"):
            outcome, _ = run(
                make_probe_module(), "housesitter.demo_sensor.probe", raw, tmp_path
            )
            line = failed(outcome, "bad_params")
            assert "is a required parameter" in line

    def test_valid_types_pass(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe",
            {"count": 3, "flag": False, "ratio": 1.5, "tags": [1], "opts": {"a": 1}},
            tmp_path,
        )
        data = echo_data(outcome)
        assert data["count"] == 3
        assert data["flag"] is False
        assert data["tags"] == [1]

    def test_error_reasons_never_carry_values(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe",
            {"count": "PLANTED-SECRET-value-1"}, tmp_path,
        )
        line = failed(outcome, "bad_params")
        assert "PLANTED-SECRET-value-1" not in line
        assert "PLANTED-SECRET-value-1" not in json.dumps(outcome.envelope)


class TestDeadline:
    def test_ctx_deadline_matches_self_deadline(self, tmp_path):
        seen: dict = {}

        def handler(self, ctx, params):
            seen["deadline"] = ctx.deadline
            return sdk.Result(data={"echo": "ok"})

        outcome, _ = run(
            make_probe_module(handler), "housesitter.demo_sensor.probe",
            {"count": 1}, tmp_path, deadline_override_s=10,
        )
        assert outcome.exit_code == 0
        assert 5 < seen["deadline"] <= 10

    def test_handler_timeout(self, tmp_path):
        def handler(self, ctx, params):
            time.sleep(5)
            return sdk.Result(data={"echo": "late"})

        outcome, logs = run(
            make_probe_module(handler), "housesitter.demo_sensor.probe",
            {"count": 1}, tmp_path, deadline_override_s=0.2,
        )
        assert outcome.exit_code == 1
        assert outcome.error_code == "timeout"
        assert outcome.error_reason == "the command did not finish within its time limit"
        assert outcome.worker_alive is True
        assert outcome.envelope["error"]["code"] == "timeout"

    def test_late_worker_log_never_reaches_stderr(self, tmp_path, monkeypatch):
        import logging as logging_mod
        import sys as sys_mod
        import threading as threading_mod

        # Late worker records must not fall through to logging.lastResort
        # after the timeout path detaches the runner's stderr handler: the
        # CLI prints the error line after run_command returns, so anything
        # reaching stderr later would land after it.
        buf = io.StringIO()
        monkeypatch.setattr(sys_mod, "stderr", buf)
        root = logging_mod.getLogger()
        saved_handlers = list(root.handlers)
        root.handlers[:] = []
        done = threading_mod.Event()
        try:
            def handler(self, ctx, params):
                try:
                    time.sleep(0.6)
                    ctx.log.warning("late-example-message")
                    return sdk.Result(data={"echo": "late"})
                finally:
                    done.set()

            outcome, logs = run(
                make_probe_module(handler), "housesitter.demo_sensor.probe",
                {"count": 1}, tmp_path, deadline_override_s=0.2,
            )
            assert outcome.exit_code == 1
            assert outcome.error_code == "timeout"
            assert done.wait(timeout=10)
            assert "late-example-message" not in logs
            assert buf.getvalue() == ""
        finally:
            root.handlers[:] = saved_handlers


class TestHandlerErrors:
    def test_command_error(self, tmp_path):
        def handler(self, ctx, params):
            raise sdk.CommandError("upstream_auth", "The login was refused.")

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        line = failed(outcome, "upstream_auth")
        assert "The login was refused." in line

    def test_upstream_error_keeps_author_message(self, tmp_path):
        def handler(self, ctx, params):
            raise UpstreamError("upstream_unreachable", "The sensor is offline.")

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        line = failed(outcome, "upstream_unreachable")
        assert "The sensor is offline." in line

    def test_not_allowed(self, tmp_path):
        def handler(self, ctx, params):
            raise NotAllowed("not allowed: GET /evil-example")

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        line = failed(outcome, "not_allowed")
        assert "allow-list" in line
        assert "/evil-example" not in line

    def test_store_error_keeps_code(self, tmp_path):
        def handler(self, ctx, params):
            raise StoreError("secret_unreadable", "cannot read it")

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        failed(outcome, "secret_unreadable")

    def test_unexpected_errors_hide_details(self, tmp_path):
        secret = "PLANTED-SECRET-abc123"
        message = f"boom {secret} from unit 'evil-instrument-example'"
        logs: list = []

        def handler(self, ctx, params):
            ctx.log.warning("about to fail")
            raise ValueError(message)

        module_cls = make_probe_module(handler)
        registry = Registry.from_classes([module_cls])
        config = make_config(tmp_path, module_cls.id)
        stream = io.StringIO()
        outcome = runner.run_command(
            "housesitter.demo_sensor.probe", {"count": 1}, registry=registry,
            env={"HOME": str(tmp_path)}, config=config, now=lambda: FIXED_NOW,
            log_stream=stream,
        )
        line = failed(outcome, "upstream_unexpected")
        assert line.endswith("the module failed unexpectedly")
        assert secret not in json.dumps(outcome.envelope)
        assert secret not in stream.getvalue()
        assert "evil-instrument-example" not in stream.getvalue()
        assert logs == []

    def test_non_result_is_unexpected(self, tmp_path):
        def handler(self, ctx, params):
            return {"echo": "not a Result"}

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        failed(outcome, "upstream_unexpected")

    def test_save_failure_maps_code(self, tmp_path, monkeypatch):
        def handler(self, ctx, params):
            return sdk.Result(data={"echo": "ok"}, save="snapshot")

        def boom(self, kind, obj, label=None):
            raise StoreError("config_invalid", "nope")

        from housesitter.core.store import FileStore

        monkeypatch.setattr(FileStore, "save", boom)
        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        failed(outcome, "config_invalid")


class TestSanitiseAndPolicy:
    COMMAND = sdk.Command(
        name="snapshot",
        handler="snapshot",
        description="Read demo sensors.",
        fields=(
            sdk.Field("data.mac", "string", identifier=True),
            sdk.Field("data.name", "string", untrusted=True),
            sdk.Field("data.count", "integer"),
            sdk.Field("data.note", "string", nullable=True),
            sdk.Field("data.aps[].mac", "string", identifier=True),
            sdk.Field("data.aps[].signal_dbm", "number"),
            sdk.Field("data.meta", "object"),
            sdk.Field("data.meta.inner", "integer"),
            sdk.Field("data.meta.ratio", "number"),
            sdk.Field("data.meta.flags", "array"),
            sdk.Field("source.site", "string"),
        ),
    )

    PAYLOAD = {
        "mac": "02:00:00:00:00:01",
        "name": "bedroom\u0000sensor\u202e-example",
        "count": 3,
        "note": None,
        "aps": [
            {"mac": "02:00:00:00:00:02", "signal_dbm": -70, "extra": "dropped"},
        ],
        "extra_top": "dropped",
        "meta": {"inner": 1, "flags": [True, None], "ratio": 1.5},
    }

    def make_module(self, payload=None, **result_kwargs):
        data = dict(self.PAYLOAD) if payload is None else payload

        def handler(self, ctx, params):
            return sdk.Result(data=data, source={"site": "default-example"},
                              **result_kwargs)

        return type(
            "PolicyModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (self.COMMAND,),
                "snapshot": handler,
            },
        )

    def test_full_identifiers_keep_everything_declared(self, tmp_path):
        outcome, _ = run(
            self.make_module(save="snapshot"), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        assert outcome.exit_code == 0
        data = outcome.envelope["data"]
        assert data["mac"] == "02:00:00:00:00:01"
        assert data["name"] == "bedroomsensor-example"
        assert data["aps"] == [{"mac": "02:00:00:00:00:02", "signal_dbm": -70}]
        assert "extra_top" not in data
        assert data["meta"] == {"inner": 1, "flags": [True, None], "ratio": 1.5}
        assert data["note"] is None
        assert outcome.envelope["source"] == {"site": "default-example"}

    def test_none_identifiers_strip_but_save_keeps(self, tmp_path):
        outcome, _ = run(
            self.make_module(save="snapshot"), "housesitter.demo_sensor.snapshot",
            {}, tmp_path, identifiers="none",
        )
        assert outcome.exit_code == 0
        data = outcome.envelope["data"]
        assert "mac" not in data
        assert data["aps"] == [{"signal_dbm": -70}]
        assert data["meta"] == {"inner": 1, "flags": [True, None], "ratio": 1.5}
        from housesitter.core.store import FileStore

        store = FileStore("demo_sensor", env={"HOME": str(tmp_path)})
        saved = store.load(outcome.envelope["saved"])
        assert saved["data"]["mac"] == "02:00:00:00:00:01"
        assert saved["data"]["aps"][0]["mac"] == "02:00:00:00:00:02"
        assert saved["data"]["extra_top"] == "dropped"
        assert saved["data"]["name"] == "bedroomsensor-example"

    def test_save_content_and_saved_id(self, tmp_path):
        outcome, _ = run(
            self.make_module(save="snapshot", label="before"),
            "housesitter.demo_sensor.snapshot", {}, tmp_path,
        )
        assert outcome.exit_code == 0
        saved_id = outcome.envelope["saved"]
        import re

        assert re.fullmatch(
            r"snapshot_\d{8}T\d{6}Z_[0-9a-f]{4}", saved_id
        )
        from housesitter.core.store import FileStore

        store = FileStore("demo_sensor", env={"HOME": str(tmp_path)})
        saved = store.load(saved_id)
        assert saved["schema"] == "housesitter.demo_sensor.snapshot/1"
        assert saved["module"] == "demo_sensor"
        assert saved["module_version"] == "0.1.0"
        assert saved["housesitter_version"] == __version__
        assert saved["collected_at"] == "2026-11-04T15:15:00Z"
        assert saved["label"] == "before"
        assert saved["data"]["mac"] == "02:00:00:00:00:01"

    def test_type_mismatch_names_path(self, tmp_path):
        payload = dict(self.PAYLOAD, count="three")
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot", {}, tmp_path
        )
        line = failed(outcome, "upstream_unexpected")
        assert "(data.count)" in line

    def test_container_mismatch_names_path(self, tmp_path):
        payload = dict(self.PAYLOAD, aps="nope")
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot", {}, tmp_path
        )
        line = failed(outcome, "upstream_unexpected")
        assert "data.aps" in line

    def test_null_container_kept(self, tmp_path):
        payload = dict(self.PAYLOAD, aps=None)
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot", {}, tmp_path
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["data"]["aps"] is None

    def test_non_nullable_null_rejected(self, tmp_path):
        command = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read demo sensors.",
            fields=(sdk.Field("data.count", "integer", nullable=False),),
        )

        def handler(self, ctx, params):
            return sdk.Result(data={"count": None})

        module_cls = type(
            "StrictModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "snapshot": handler,
            },
        )
        outcome, _ = run(module_cls, "housesitter.demo_sensor.snapshot", {}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert "(data.count)" in line


LONG_LABEL = "l" * 500


class TestEnvelopeDetails:
    def test_label_capped_at_200(self, tmp_path):
        def handler(self, ctx, params):
            return sdk.Result(data={"echo": "ok"}, label=LONG_LABEL)

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        assert outcome.exit_code == 0
        assert len(outcome.envelope["label"]) == 200

    def test_blank_label_omitted(self, tmp_path):
        def handler(self, ctx, params):
            return sdk.Result(data={"echo": "ok"}, label="\u0000\u0001")

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        assert outcome.exit_code == 0
        assert "label" not in outcome.envelope

    def test_collected_at_defaults_to_now(self, tmp_path):
        outcome, _ = run(
            make_probe_module(), "housesitter.demo_sensor.probe", {"count": 1}, tmp_path
        )
        assert outcome.envelope["collected_at"] == "2026-11-04T15:15:00Z"

    def test_result_collected_at_used(self, tmp_path):
        seen = datetime(2026, 11, 5, 6, 7, 8, tzinfo=timezone.utc)

        def handler(self, ctx, params):
            return sdk.Result(data={"echo": "ok"}, collected_at=seen)

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        assert outcome.envelope["collected_at"] == "2026-11-05T06:07:08Z"

    def test_warnings_capped_with_overflow_line(self, tmp_path):
        def handler(self, ctx, params):
            ctx.warnings.append("ctx-note")
            ctx.warnings.append("ctx-note")
            return sdk.Result(
                data={"echo": "ok"}, warnings=[f"w{n}" for n in range(25)]
            )

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        warnings = outcome.envelope["warnings"]
        assert len(warnings) == 20
        assert warnings[:19] == [f"w{n}" for n in range(19)]
        assert warnings[-1] == "7 more warnings omitted"

    def test_warnings_deduped_and_cleaned(self, tmp_path):
        def handler(self, ctx, params):
            ctx.warnings.append(7)
            return sdk.Result(
                data={"echo": "ok"},
                warnings=["a", "a", "\u0000", "b" * 300],
            )

        outcome, _ = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                          {"count": 1}, tmp_path)
        warnings = outcome.envelope["warnings"]
        assert warnings[0] == "a"
        assert warnings.count("a") == 1
        assert len(warnings[1]) == 200
        assert all(isinstance(item, str) for item in warnings)

    def test_log_lines_follow_format_and_cap(self, tmp_path):
        def handler(self, ctx, params):
            ctx.log.warning("hello %s", "there")
            ctx.log.warning("noisy\t\x00%s", "x" * 500)
            return sdk.Result(data={"echo": "ok"})

        outcome, logs = run(make_probe_module(handler), "housesitter.demo_sensor.probe",
                             {"count": 1}, tmp_path)
        assert outcome.exit_code == 0
        assert logs == "".join(line + "\n" for line in outcome.log_lines)
        assert outcome.log_lines[0] == "housesitter: demo_sensor: log: hello there"
        assert outcome.log_lines[1].startswith("housesitter: demo_sensor: log: noisy x")
        assert len(outcome.log_lines[1]) <= 400
        assert "\t" not in outcome.log_lines[1]


PAGED_COMMAND = sdk.Command(
    name="get",
    handler="get",
    description="Page demo readings.",
    optional={
        "section": sdk.Param("string", "Which list to continue."),
        "offset": sdk.Param("integer", "Where to continue."),
        "id": sdk.Param("string", "Saved result id."),
    },
    fields=(
        sdk.Field("data.items[]", "integer"),
        sdk.Field("data.note", "string", nullable=True),
    ),
)


def make_paged_module(items, note="n", paging_lists=("items",), save="snapshot",
                      compact=None, start=None, paging_params=None,
                      paging_command="get"):
    def handler(self, ctx, params):
        return sdk.Result(
            data={"items": items, "note": note},
            compact=compact,
            paging=sdk.Paging(
                command=paging_command, lists=paging_lists,
                params=dict(paging_params) if paging_params is not None
                else {"kind": "full"},
                start=start or {},
            ),
            save=save,
        )

    return type(
        "PagedModule", (sdk.Module,),
        {
            "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
            "config": {}, "http_allow": (), "commands": (PAGED_COMMAND,),
            "get": handler,
        },
    )


class TestBudget:
    def full_name(self):
        return "housesitter.demo_sensor.get"

    def test_small_result_fits_whole(self, tmp_path):
        outcome, _ = run(make_paged_module([1, 2, 3]), self.full_name(), {}, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is False
        assert outcome.envelope["more"] is None
        assert outcome.envelope["data"] == {"items": [1, 2, 3], "note": "n"}

    def test_compact_used_when_full_too_big(self, tmp_path):
        big = list(range(30000))
        outcome, _ = run(
            make_paged_module(big, compact={"items": [1, 2]}),
            self.full_name(), {}, tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is False
        assert outcome.envelope["data"] == {"items": [1, 2]}

    def test_truncation_pages_first_list(self, tmp_path):
        big = list(range(30000))
        start = {"items": 50}
        outcome, _ = run(
            make_paged_module(big, compact={"items": big}, start=start),
            self.full_name(), {}, tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is True
        data = outcome.envelope["data"]
        kept = len(data["items"])
        assert 0 < kept < len(big)
        assert data["items"] == big[:kept]
        more = outcome.envelope["more"]
        assert more["command"] == "housesitter.demo_sensor.get"
        assert more["params"]["section"] == "items"
        assert more["params"]["offset"] == 50 + kept
        assert more["remaining"] == {"items": len(big) - kept}
        assert "remaining" not in more["params"]
        assert more["params"]["id"] == outcome.envelope["saved"]
        assert more["params"]["kind"] == "full"
        from housesitter.core import envelope as envelope_mod

        assert envelope_mod.measure(
            envelope_mod.serialize(outcome.envelope)
        ) <= envelope_mod.BUDGET_BYTES

    def test_truncation_moves_to_next_list(self, tmp_path):
        command = sdk.Command(
            name="get", handler="get", description="Page.",
            optional={
                "section": sdk.Param("string", "Which list to continue."),
                "offset": sdk.Param("integer", "Where to continue."),
                "id": sdk.Param("string", "Saved result id."),
            },
            fields=(sdk.Field("data.a[]", "integer"), sdk.Field("data.b[]", "integer")),
        )

        def handler(self, ctx, params):
            return sdk.Result(
                data={"a": [1], "b": list(range(30000))},
                paging=sdk.Paging(command="get", lists=("a", "b")),
                save="snapshot",
            )

        module_cls = type(
            "TwoListModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "get": handler,
            },
        )
        outcome, _ = run(module_cls, self.full_name(), {}, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is True
        more = outcome.envelope["more"]
        assert more["params"]["section"] == "a"
        assert outcome.envelope["data"]["a"] == []
        assert more["remaining"]["a"] == 1
        assert more["remaining"]["b"] > 0
        assert more["params"]["offset"] == 0

    def test_too_big_without_paging(self, tmp_path):
        module_cls = make_paged_module(list(range(30000)))
        # Drop paging by rebuilding without it.
        def handler(self, ctx, params):
            return sdk.Result(data={"items": list(range(30000)), "note": "n"})

        module_cls = type(
            "NoPageModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (PAGED_COMMAND,),
                "get": handler,
            },
        )
        outcome, _ = run(module_cls, self.full_name(), {}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert "too large for the output limit" in line

    def test_non_ascii_measured_by_sdk_formula(self, tmp_path):
        from housesitter.core import envelope as envelope_mod

        note = "\u00e9" * 30000

        def handler(self, ctx, params):
            return sdk.Result(
                data={"items": [1], "note": note},
                compact={"items": [1], "note": "small caf\u00e9"},
            )

        module_cls = type(
            "UnicodeModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (PAGED_COMMAND,),
                "get": handler,
            },
        )
        outcome, _ = run(module_cls, self.full_name(), {}, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == {"items": [1], "note": "small caf\u00e9"}
        # The raw output keeps non-ASCII characters unescaped ...
        text = envelope_mod.serialize(outcome.envelope)
        assert "caf\u00e9" in text
        assert envelope_mod.measure(text) <= envelope_mod.BUDGET_BYTES
        # ... while the full form only fits by the naive length: the SDK's
        # escaped measure is what rejects it.
        full_env = envelope_mod.build_ok(
            schema="housesitter.demo_sensor.get/1",
            module="demo_sensor",
            module_version="0.1.0",
            housesitter_version=__version__,
            collected_at="2026-11-04T15:15:00Z",
            source={},
            data={"items": [1], "note": note},
            warnings=[],
            truncated=False,
            saved=None,
            more=None,
        )
        full_text = envelope_mod.serialize(full_env)
        assert len(full_text) < envelope_mod.BUDGET_BYTES
        assert envelope_mod.measure(full_text) > envelope_mod.BUDGET_BYTES

    def test_no_save_uses_paging_params_id(self, tmp_path):
        big = list(range(30000))
        outcome, _ = run(
            make_paged_module(
                big, compact={"items": big}, save=None,
                paging_params={"kind": "full", "id": "snapshot_replay"},
            ),
            self.full_name(), {}, tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["saved"] is None
        assert (
            outcome.envelope["more"]["params"]["id"] == "snapshot_replay"
        )

    def test_truncation_without_fetchable_paging_fails(self, tmp_path):
        big = list(range(30000))
        outcome, _ = run(
            make_paged_module(big, compact={"items": big}, save=None),
            self.full_name(), {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert (
            "the module's paging cannot fetch the rest of this result"
            in line
        )

    def test_unknown_paging_command_fails(self, tmp_path):
        big = list(range(30000))
        outcome, _ = run(
            make_paged_module(big, paging_command="missing"),
            self.full_name(), {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert (
            "the module's paging cannot fetch the rest of this result"
            in line
        )

    def test_paging_command_with_wrong_param_types_fails(self, tmp_path):
        command = sdk.Command(
            name="get", handler="get", description="Page.",
            optional={
                "section": sdk.Param("string", "Which list."),
                "offset": sdk.Param("string", "Where to continue."),
                "id": sdk.Param("string", "Saved result id."),
            },
            fields=(sdk.Field("data.items[]", "integer"),),
        )

        def handler(self, ctx, params):
            return sdk.Result(
                data={"items": list(range(30000))},
                paging=sdk.Paging(command="get", lists=("items",)),
                save="snapshot",
            )

        module_cls = type(
            "MistypedModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "get": handler,
            },
        )
        outcome, _ = run(module_cls, self.full_name(), {}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert (
            "the module's paging cannot fetch the rest of this result"
            in line
        )

    def test_paging_command_without_paging_params_fails(self, tmp_path):
        command = sdk.Command(
            name="get", handler="get", description="Page.",
            fields=(sdk.Field("data.items[]", "integer"),),
        )

        def handler(self, ctx, params):
            return sdk.Result(
                data={"items": list(range(30000))},
                paging=sdk.Paging(command="get", lists=("items",)),
                save="snapshot",
            )

        module_cls = type(
            "UnpageableModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "get": handler,
            },
        )
        outcome, _ = run(module_cls, self.full_name(), {}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert (
            "the module's paging cannot fetch the rest of this result"
            in line
        )

    def test_nan_in_paging_params_is_not_valid_json(self, tmp_path):
        # Paging params carry no field declarations, so a non-finite float
        # is the one route that reaches the envelope unserialisable.
        big = list(range(30000))
        outcome, _ = run(
            make_paged_module(
                big, compact={"items": big},
                paging_params={"kind": "full", "n": float("nan")},
            ),
            self.full_name(), {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert "the result is not valid JSON" in line


class TestCorePaging:
    """Core commands page through ``housesitter.<command>`` and CORE_COMMANDS."""

    def test_core_truncation_points_at_core_command(self, tmp_path, monkeypatch):
        from housesitter.core import builtins as builtins_mod

        items = list(range(30000))
        monkeypatch.setitem(
            builtins_mod.CORE_COMMANDS,
            "describe",
            sdk.Command(
                name="describe",
                handler="describe",
                description="List installed Housesitter modules.",
                optional={
                    "section": sdk.Param("string", "Which list to continue."),
                    "offset": sdk.Param("integer", "Where to continue."),
                    "id": sdk.Param("string", "Saved result id."),
                },
                fields=(sdk.Field("data.items[]", "integer"),),
            ),
        )

        def fake_describe(self, ctx, params):
            return sdk.Result(
                data={"items": items},
                paging=sdk.Paging(command="describe", lists=("items",)),
                save="snapshot",
            )

        monkeypatch.setattr(
            builtins_mod.CoreCommands, "describe", fake_describe
        )
        outcome, _ = run(
            make_probe_module(), "housesitter.describe", {}, tmp_path
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is True
        more = outcome.envelope["more"]
        assert more["command"] == "housesitter.describe"
        assert more["params"]["section"] == "items"
        assert more["params"]["id"] == outcome.envelope["saved"]

    def test_core_unknown_paging_command_fails(self, tmp_path, monkeypatch):
        from housesitter.core import builtins as builtins_mod

        monkeypatch.setitem(
            builtins_mod.CORE_COMMANDS,
            "describe",
            sdk.Command(
                name="describe",
                handler="describe",
                description="List installed Housesitter modules.",
                optional={
                    "section": sdk.Param("string", "Which list to continue."),
                    "offset": sdk.Param("integer", "Where to continue."),
                    "id": sdk.Param("string", "Saved result id."),
                },
                fields=(sdk.Field("data.items[]", "integer"),),
            ),
        )

        def fake_describe(self, ctx, params):
            return sdk.Result(
                data={"items": list(range(30000))},
                paging=sdk.Paging(command="missing", lists=("items",)),
                save="snapshot",
            )

        monkeypatch.setattr(
            builtins_mod.CoreCommands, "describe", fake_describe
        )
        outcome, _ = run(
            make_probe_module(), "housesitter.describe", {}, tmp_path
        )
        line = failed(outcome, "upstream_unexpected", module="housesitter")
        assert (
            "the module's paging cannot fetch the rest of this result"
            in line
        )


class TestPagingReplay:
    """A paging command must fetch the rest, round after round."""

    def make_module(self, lines_a, lines_b=None):
        fields = [sdk.Field("data.a[]", "string", untrusted=True)]
        if lines_b is not None:
            fields.append(sdk.Field("data.b[]", "string", untrusted=True))
        snapshot_fields = tuple(fields)
        get_command = sdk.Command(
            name="get",
            handler="get",
            description="Page demo readings.",
            required={
                "section": sdk.Param("string", "Which list to continue."),
                "offset": sdk.Param("integer", "Where to continue."),
                "id": sdk.Param("string", "Saved result id."),
            },
            fields=snapshot_fields,
        )

        def snapshot_one(self, ctx, params):
            return sdk.Result(
                data={"a": lines_a},
                paging=sdk.Paging(command="get", lists=("a",)),
                save="snapshot",
            )

        def snapshot_two(self, ctx, params):
            return sdk.Result(
                data={"a": lines_a, "b": lines_b},
                paging=sdk.Paging(command="get", lists=("a", "b")),
                save="snapshot",
            )

        def get(self, ctx, params):
            saved = ctx.store.load(params["id"])
            section = params["section"]
            offset = params["offset"]
            rest = saved["data"][section][offset:]
            return sdk.Result(
                data={section: rest},
                paging=sdk.Paging(
                    command="get", lists=(section,),
                    params={"id": params["id"]},
                    start={section: offset},
                ),
            )

        commands = (
            sdk.Command(
                name="snapshot_one", handler="snapshot_one",
                description="Read demo sensors.", fields=snapshot_fields,
            ),
            sdk.Command(
                name="snapshot_two", handler="snapshot_two",
                description="Read demo sensors.", fields=snapshot_fields,
            ),
            get_command,
        )
        return type(
            "ReplayModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": commands,
                "snapshot_one": snapshot_one,
                "snapshot_two": snapshot_two,
                "get": get,
            },
        )

    def replay(self, module_cls, outcome, tmp_path, first_section):
        seen: dict = {}
        section = first_section
        while outcome.envelope["truncated"]:
            more = outcome.envelope["more"]
            assert (
                more["command"] == "housesitter.demo_sensor.get"
            )
            params = dict(more["params"])
            section = params["section"]
            outcome, _ = run(
                module_cls, more["command"], params, tmp_path
            )
            assert outcome.exit_code == 0
            seen.setdefault(section, []).extend(
                outcome.envelope["data"][section]
            )
        return seen

    def test_single_non_ascii_list_replays_exactly(self, tmp_path):
        lines = [f"résumé-{index:05d}-example" for index in range(6000)]
        module_cls = self.make_module(lines)
        outcome, _ = run(
            module_cls, "housesitter.demo_sensor.snapshot_one", {},
            tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is True
        first = list(outcome.envelope["data"]["a"])
        replayed = self.replay(module_cls, outcome, tmp_path, "a")
        assert first + replayed["a"] == lines

    def test_two_lists_replay_exactly(self, tmp_path):
        lines_a = [f"alpha-{index:05d}-example" for index in range(6000)]
        lines_b = [f"ßeta-{index:05d}-example" for index in range(6000)]
        module_cls = self.make_module(lines_a, lines_b)
        outcome, _ = run(
            module_cls, "housesitter.demo_sensor.snapshot_two", {},
            tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["truncated"] is True
        more = outcome.envelope["more"]
        assert set(more["remaining"]) == {"a", "b"}
        first_a = list(outcome.envelope["data"]["a"])
        first_b = list(outcome.envelope["data"]["b"])
        saved_id = outcome.envelope["saved"]
        replayed = self.replay(module_cls, outcome, tmp_path, "a")
        assert first_a + replayed.get("a", []) == lines_a
        # The snapshot kept the head of b; fetch its tail directly.
        tail, _ = run(
            module_cls, "housesitter.demo_sensor.get",
            {"section": "b", "offset": len(first_b), "id": saved_id},
            tmp_path,
        )
        assert tail.exit_code == 0
        rest = list(tail.envelope["data"]["b"])
        while tail.envelope["truncated"]:
            more_b = tail.envelope["more"]
            tail, _ = run(
                module_cls, more_b["command"], dict(more_b["params"]),
                tmp_path,
            )
            assert tail.exit_code == 0
            rest.extend(tail.envelope["data"]["b"])
        assert first_b + rest == lines_b


class TestOpaqueArrays:
    """Opaque leaves exist only as arrays of plain scalars and nested arrays."""

    COMMAND = sdk.Command(
        name="snapshot",
        handler="snapshot",
        description="Read demo sensors.",
        fields=(sdk.Field("data.tags", "array"),),
    )

    def make_module(self, payload):
        def handler(self, ctx, params):
            return sdk.Result(data=payload)

        return type(
            "OpaqueModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (self.COMMAND,),
                "snapshot": handler,
            },
        )

    def test_scalar_only_leaves_kept(self, tmp_path):
        payload = {"tags": [1, True, None, [2, [3]]]}
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == payload

    def test_string_item_rejected(self, tmp_path):
        payload = {"tags": ["nope-example"]}
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert "(data.tags)" in line

    def test_object_item_rejected(self, tmp_path):
        payload = {"tags": [{"a": 1}]}
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert "(data.tags)" in line

    def test_nan_item_rejected(self, tmp_path):
        payload = {"tags": [float("nan")]}
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert "(data.tags)" in line

    def test_infinite_item_rejected(self, tmp_path):
        payload = {"tags": [float("inf")]}
        outcome, _ = run(
            self.make_module(payload), "housesitter.demo_sensor.snapshot",
            {}, tmp_path,
        )
        line = failed(outcome, "upstream_unexpected")
        assert "(data.tags)" in line

    def test_opaque_object_leaf_rejected_at_definition(self, tmp_path):
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read demo sensors.",
                fields=(sdk.Field("data.meta", "object"),),
            )

    def test_nan_in_saved_result_is_unexpected(self, tmp_path):
        def handler(self, ctx, params):
            return sdk.Result(
                data={"tags": [float("nan")]}, save="snapshot"
            )

        module_cls = type(
            "OpaqueSaveModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (self.COMMAND,),
                "snapshot": handler,
            },
        )
        outcome, _ = run(
            module_cls, "housesitter.demo_sensor.snapshot", {}, tmp_path
        )
        line = failed(outcome, "upstream_unexpected")
        assert "the module failed unexpectedly" in line


class TestItemStringLists:
    def make_module(self, fields, payload, identifiers="full"):
        command = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read demo sensors.",
            fields=fields,
        )

        def handler(self, ctx, params):
            return sdk.Result(data=payload)

        return type(
            "ItemListModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "snapshot": handler,
            },
        )

    def run_snapshot(self, fields, payload, tmp_path, identifiers="full"):
        return run(
            self.make_module(fields, payload),
            "housesitter.demo_sensor.snapshot", {}, tmp_path,
            identifiers=identifiers,
        )

    def test_untrusted_items_sanitised(self, tmp_path):
        fields = (sdk.Field("data.tags[]", "string", untrusted=True, max_len=8),)
        payload = {"tags": ["ok-example", "bad\x00thing", "toolonggggggg"]}
        outcome, _ = self.run_snapshot(fields, payload, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == {
            "tags": ["ok-examp", "badthing", "toolongg"]
        }

    def test_identifier_items_kept_when_full(self, tmp_path):
        fields = (sdk.Field("data.tags[]", "string", identifier=True),)
        payload = {"tags": ["02:00:00:00:00:01"]}
        outcome, _ = self.run_snapshot(fields, payload, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == payload

    def test_identifier_items_remove_list_when_none(self, tmp_path):
        fields = (sdk.Field("data.tags[]", "string", identifier=True),)
        payload = {"tags": ["02:00:00:00:00:01"]}
        outcome, _ = self.run_snapshot(
            fields, payload, tmp_path, identifiers="none"
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == {}

    def test_declared_list_and_items_together(self, tmp_path):
        fields = (
            sdk.Field("data.tags", "array", identifier=True),
            sdk.Field("data.tags[]", "string"),
        )
        payload = {"tags": ["a-example", "b-example"]}
        outcome, _ = self.run_snapshot(fields, payload, tmp_path)
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == payload
        outcome, _ = self.run_snapshot(
            fields, payload, tmp_path, identifiers="none"
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == {}


class TestFieldTreeNullability:
    def run_snapshot(self, fields, payload, tmp_path):
        command = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read demo sensors.",
            fields=fields,
        )

        def handler(self, ctx, params):
            return sdk.Result(data=payload)

        module_cls = type(
            "NullModule", (sdk.Module,),
            {
                "id": "demo_sensor", "title": "Demo", "version": "0.1.0",
                "config": {}, "http_allow": (), "commands": (command,),
                "snapshot": handler,
            },
        )
        return run(
            module_cls, "housesitter.demo_sensor.snapshot", {}, tmp_path
        )

    def test_non_nullable_list_null_rejected(self, tmp_path):
        fields = (
            sdk.Field("data.aps", "array", nullable=False),
            sdk.Field("data.aps[].x", "string"),
        )
        outcome, _ = self.run_snapshot(fields, {"aps": None}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert "(data.aps)" in line

    def test_non_nullable_object_null_rejected(self, tmp_path):
        fields = (
            sdk.Field("data.meta", "object", nullable=False),
            sdk.Field("data.meta.a", "string"),
        )
        outcome, _ = self.run_snapshot(fields, {"meta": None}, tmp_path)
        line = failed(outcome, "upstream_unexpected")
        assert "(data.meta)" in line

    def test_nullable_containers_accept_null(self, tmp_path):
        fields = (
            sdk.Field("data.aps", "array"),
            sdk.Field("data.aps[].x", "string"),
            sdk.Field("data.meta", "object"),
            sdk.Field("data.meta.a", "string"),
        )
        outcome, _ = self.run_snapshot(
            fields, {"aps": None, "meta": None}, tmp_path
        )
        assert outcome.exit_code == 0
        assert outcome.envelope["data"] == {"aps": None, "meta": None}


class TestNullIdentifiersOmitted:
    """``identifiers: none`` leaves identifier keys out even when null."""

    @pytest.mark.parametrize(
        "fields, data",
        [
            ((sdk.Field("data.mac", "string", identifier=True),), {"mac": None}),
            (
                (
                    sdk.Field("data.aps", "array", identifier=True),
                    sdk.Field("data.aps[].x", "integer"),
                ),
                {"aps": None},
            ),
            (
                (
                    sdk.Field("data.o", "object", identifier=True),
                    sdk.Field("data.o.x", "integer"),
                ),
                {"o": None},
            ),
            (
                (sdk.Field("data.ips[]", "string", identifier=True),),
                {"ips": None},
            ),
        ],
    )
    def test_null_identifier_key_is_dropped(self, fields, data):
        tree = runner._build_tree(list(fields))
        assert runner._project_node(tree, data, "data", True) == {}
        # With identifiers=full the null is kept as declared.
        assert runner._project_node(tree, data, "data", False) == data


class TestNestedNonFiniteParams:
    """Non-finite numbers are refused inside object and array params too."""

    @pytest.mark.parametrize(
        "param, value",
        [
            (sdk.Param("object", "Options."), {"x": float("nan")}),
            (sdk.Param("array", "Values."), [1, [float("inf")]]),
        ],
    )
    def test_nested_non_finite_is_bad_params(self, param, value):
        command = sdk.Command(
            "probe", handler="probe", description="Probe.", optional={"p": param}
        )
        with pytest.raises(sdk.CommandError) as excinfo:
            runner.validate_params(command, {"p": value})
        assert excinfo.value.code == "bad_params"

    def test_nested_non_finite_default_rejected(self):
        with pytest.raises(ValueError):
            sdk.Param("array", "Values.", default=[float("nan")])
