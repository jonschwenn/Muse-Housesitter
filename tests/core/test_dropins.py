"""Pin drop-in generation, validation, budgets, and exec-path rules."""

from __future__ import annotations

import os
import stat

import pytest

from housesitter import sdk
from housesitter.core import builtins as core_builtins
from housesitter.core.dropins import (
    DROPINS_BUDGET_BYTES,
    DropinError,
    check_exec_path,
    command_entry,
    generate,
    serialize,
    validate,
    write_files,
)
from housesitter.core.registry import Registry


def make_module(module_id="demo_sensor", commands=None, **overrides):
    if commands is None:
        commands = (
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read demo sensors.",
                required={
                    "count": sdk.Param("integer", "How many readings.", minimum=1)
                },
                optional={
                    "label": sdk.Param("string", "Note about this reading.", default="x")
                },
                timeout_ms=15000,
                fields=(sdk.Field("data.temperature_c", "number"),),
            ),
        )
    attrs = {
        "id": module_id,
        "title": "Demo sensor",
        "version": "0.1.0",
        "config": {},
        "http_allow": (),
        "commands": commands,
        "snapshot": lambda self, ctx, params: sdk.Result(data={}),
    }
    attrs.update(overrides)
    return type(f"Module_{module_id}", (sdk.Module,), attrs)


@pytest.fixture()
def registry():
    return Registry.from_classes([make_module()])


class TestGenerate:
    def test_covers_module_and_core_commands(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        assert sorted(files) == [
            "housesitter.demo_sensor.snapshot.json",
            "housesitter.describe.json",
            "housesitter.doctor.json",
        ]

    def test_entry_shape(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        entry = files["housesitter.demo_sensor.snapshot.json"]
        assert entry == {
            "description": "Read demo sensors.",
            "exec": ["/opt/housesitter/bin/housesitter", "invoke",
                     "housesitter.demo_sensor.snapshot"],
            "required": {
                "count": {
                    "type": "integer",
                    "description": "How many readings. Minimum 1.",
                }
            },
            "optional": {
                "label": {
                    "type": "string",
                    "description": "Note about this reading. Default x.",
                }
            },
            "timeout_ms": 15000,
        }

    def test_param_specs_carry_only_type_and_description(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        for content in files.values():
            for section in ("required", "optional"):
                for spec in content[section].values():
                    assert sorted(spec) == ["description", "type"]

    def test_generated_files_all_validate(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        for filename, content in files.items():
            validate(filename[:-5], content)

    def test_rejects_bad_exec_path(self, registry):
        with pytest.raises(DropinError):
            generate(registry, "relative/bin/housesitter")

    def test_total_budget_enforced(self):
        commands = tuple(
            sdk.Command(
                name=f"cmd{n:03d}", handler="snapshot", description="Read demo sensors."
            )
            for n in range(600)
        )
        big = Registry.from_classes([make_module("big_sensor", commands=commands)])
        with pytest.raises(DropinError):
            generate(big, "/opt/housesitter/bin/housesitter")
        # But each file on its own is fine.
        files = generate(
            Registry.from_classes([make_module()]), "/opt/housesitter/bin/housesitter"
        )
        total = sum(len(serialize(content).encode()) for content in files.values())
        assert total <= DROPINS_BUDGET_BYTES


class TestSerialize:
    def test_compact_sorted_with_newline(self):
        data = {"b": [1, 2], "a": "x"}
        assert serialize(data) == '{"a":"x","b":[1,2]}\n'

    def test_non_ascii_not_escaped(self):
        assert serialize({"name": "caf\u00e9-example"}) == '{"name":"caf\u00e9-example"}\n'

    def test_bytes_match_measured_budget(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        total = sum(len(serialize(content).encode("utf-8")) for content in files.values())
        assert total <= DROPINS_BUDGET_BYTES


class TestCheckExecPath:
    @pytest.mark.parametrize(
        "bad",
        [
            "relative/path",
            "/opt/housesitter//bin/housesitter",
            "/opt/housesitter/./bin/housesitter",
            "/opt/housesitter/bin/housesitter ",
            "/opt/housesitter/bin/house sitter",
            "/opt/housesitter/bin/house\tsitter",
            "",
        ],
    )
    def test_rejected(self, bad):
        with pytest.raises(DropinError):
            check_exec_path(bad)

    def test_accepted(self):
        assert check_exec_path("/opt/housesitter/bin/housesitter") == (
            "/opt/housesitter/bin/housesitter"
        )


def valid_entry(**overrides):
    entry = {
        "description": "Read demo sensors.",
        "exec": ["/opt/housesitter/bin/housesitter", "invoke", "housesitter.x.y"],
        "required": {"count": {"type": "integer", "description": "How many."}},
        "optional": {},
        "timeout_ms": 30000,
    }
    entry.update(overrides)
    return entry


class TestValidate:
    def test_minimal_valid(self):
        validate("housesitter.x.y", valid_entry())

    @pytest.mark.parametrize(
        "bad_name",
        [
            "NoDot",
            "Housesitter.x.y",
            "housesitter.x-y",
            "x",
            "a" * 30 + "." + "b" * 34,  # 65 chars
            "system.reboot.now",
            "file.read.stuff",
            "device.camera.snap",
            "link.register.thing",
        ],
    )
    def test_bad_names_rejected(self, bad_name):
        with pytest.raises(DropinError):
            validate(bad_name, valid_entry())

    def test_non_object_rejected(self):
        with pytest.raises(DropinError):
            validate("housesitter.x.y", [])

    @pytest.mark.parametrize(
        "entry",
        [
            valid_entry(description="  "),
            valid_entry(description=""),
            valid_entry(description=None),
            valid_entry(exec=[]),
            valid_entry(exec="not-a-list"),
            valid_entry(exec=["", "invoke", "x"]),
            valid_entry(exec=["relative/path", "invoke", "x"]),
            valid_entry(required={"a-b": {"type": "string", "description": "D."}}),
            valid_entry(
                required={"a": {"type": "string", "description": "D.", "extra": 1}}
            ),
            valid_entry(required={"a": {"type": "string"}}),
            valid_entry(required={"a": {"type": "frozen", "description": "D."}}),
            valid_entry(required={"a": {"type": "string", "description": "  "}}),
            valid_entry(
                required={"a": {"type": "string", "description": "D."}},
                optional={"a": {"type": "string", "description": "D."}},
            ),
            valid_entry(timeout_ms=0),
            valid_entry(timeout_ms=600001),
            valid_entry(timeout_ms=True),
            valid_entry(timeout_ms="30000"),
            valid_entry(surprise=1),
        ],
    )
    def test_bad_entries_rejected(self, entry):
        with pytest.raises(DropinError):
            validate("housesitter.x.y", entry)

    def test_timeout_bounds_ok(self):
        validate("housesitter.x.y", valid_entry(timeout_ms=1))
        validate("housesitter.x.y", valid_entry(timeout_ms=600000))

    def test_subset_of_keys_ok(self):
        validate("housesitter.x.y", {"description": "Read things.", "exec": ["/bin/x"]})

    def test_per_file_budget(self):
        entry = valid_entry(description="x" * (64 * 1024))
        with pytest.raises(DropinError, match="65536"):
            validate("housesitter.x.y", entry)


class TestWriteFiles:
    def test_writes_sorted_with_mode_644(self, tmp_path, registry):
        out = tmp_path / "commands.d"
        out.mkdir()
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        names = write_files(files, out)
        assert names == sorted(files)
        for name in names:
            path = out / name
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o644
            assert path.read_bytes() == serialize(files[name]).encode("utf-8")

    def test_no_temp_files_left(self, tmp_path, registry):
        out = tmp_path / "commands.d"
        out.mkdir()
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        write_files(files, out)
        assert sorted(os.listdir(out)) == sorted(files)

    def test_temp_names_are_unpredictable(self, tmp_path, registry, monkeypatch):
        import re

        out = tmp_path / "commands.d"
        out.mkdir()
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(src)
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", spy)
        write_files(files, out)
        assert len(seen) == len(files)
        for src in seen:
            assert re.fullmatch(r".*\.tmp-[0-9a-f]{16}", src) is not None
            assert f".tmp-{os.getpid()}" not in src
            assert not os.path.exists(src)

    def test_failed_write_unlinks_temp(self, tmp_path, registry, monkeypatch):
        out = tmp_path / "commands.d"
        out.mkdir()
        files = generate(registry, "/opt/housesitter/bin/housesitter")

        def boom(src, dst):
            raise OSError("cannot install")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            write_files(files, out)
        assert os.listdir(out) == []

    def test_dest_symlink_is_replaced_not_followed(
        self, tmp_path, registry
    ):
        out = tmp_path / "commands.d"
        out.mkdir()
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        name = sorted(files)[0]
        target = tmp_path / "target.json"
        target.write_bytes(b'{"keep": true}')
        os.symlink(target, out / name)
        write_files(files, out)
        assert target.read_bytes() == b'{"keep": true}'
        path = out / name
        assert not os.path.islink(path)
        assert path.read_bytes() == serialize(files[name]).encode("utf-8")


class TestCoreEntries:
    def test_core_commands_have_entries(self, registry):
        files = generate(registry, "/opt/housesitter/bin/housesitter")
        describe = files["housesitter.describe.json"]
        assert describe["exec"][2] == "housesitter.describe"
        assert describe["timeout_ms"] == core_builtins.CORE_COMMANDS["describe"].timeout_ms
        doctor = files["housesitter.doctor.json"]
        assert doctor["optional"]["check"] == {
            "type": "boolean",
            "description": core_builtins.CORE_COMMANDS["doctor"]
            .optional["check"]
            .drop_in_description(),
        }

    def test_command_entry_helper(self):
        command = sdk.Command(
            name="snapshot", handler="snapshot", description="Read demo sensors."
        )
        entry = command_entry("housesitter.demo_sensor.snapshot", command, "/bin/hs")
        assert entry["exec"] == ["/bin/hs", "invoke", "housesitter.demo_sensor.snapshot"]
        assert entry["required"] == {}
