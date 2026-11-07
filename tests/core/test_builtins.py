"""Pin describe and doctor output, findings, and skill handling."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from housesitter import sdk
from housesitter.core import dropins as dropins_mod
from housesitter.core.builtins import CoreCommands
from housesitter.core.config import Config, ModuleConfig
from housesitter.core.registry import Registry


def make_module(module_id="demo_sensor", **overrides):
    attrs = {
        "id": module_id,
        "title": "Demo sensor",
        "version": "0.1.0",
        "config": {},
        "http_allow": (),
        "commands": (
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read demo sensors.",
                fields=(sdk.Field("data.temperature_c", "number"),),
            ),
        ),
        "snapshot": lambda self, ctx, params: sdk.Result(data={}),
    }
    attrs.update(overrides)
    return type(f"Module_{module_id}", (sdk.Module,), attrs)


def make_config(tmp_path: Path, modules=None) -> Config:
    return Config(
        path=tmp_path / ".config" / "housesitter" / "config.json",
        secrets_dir=tmp_path / "secrets",
        identifiers="full",
        retention=500,
        modules=modules or {},
    )


def make_pkg(tmp_path: Path, monkeypatch, name: str, files: dict) -> str:
    pkgdir = tmp_path / name
    pkgdir.mkdir(exist_ok=True)
    (pkgdir / "__init__.py").write_text("")
    for filename, content in files.items():
        (pkgdir / filename).write_text(content)
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    importlib.import_module(name)
    return name


class TestDescribe:
    def run_describe(self, tmp_path, registry, config):
        impl = CoreCommands(registry, config, {"HOME": str(tmp_path)})
        return impl.describe(None, {})

    def test_lists_modules_and_core(self, tmp_path):
        module_cls = make_module()
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = self.run_describe(tmp_path, registry, config)
        (entry,) = result.data["modules"]
        assert entry["id"] == "demo_sensor"
        assert entry["title"] == "Demo sensor"
        assert entry["version"] == "0.1.0"
        assert entry["third_party"] is False
        assert entry["enabled"] is True
        assert entry["skill"] is None  # no SKILL.md next to the test module
        (command,) = entry["commands"]
        assert command["name"] == "housesitter.demo_sensor.snapshot"
        assert command["timeout_ms"] == 30000
        assert command["required"] == []
        assert command["optional"] == []
        names = [item["name"] for item in result.data["core_commands"]]
        assert names == ["housesitter.describe", "housesitter.doctor"]
        assert any("SKILL.md for demo_sensor is missing" in w for w in result.warnings)

    def test_command_params_are_sorted_lists(self, tmp_path):
        module_cls = make_module(
            commands=(
                sdk.Command(
                    name="snapshot",
                    handler="snapshot",
                    description="Read demo sensors.",
                    required={"zeta": sdk.Param("integer", "Last.")},
                    optional={
                        "beta": sdk.Param("string", "Second."),
                        "alpha": sdk.Param("boolean", "First."),
                    },
                ),
            )
        )
        registry = Registry.from_classes([module_cls])
        config = make_config(tmp_path)
        result = self.run_describe(tmp_path, registry, config)
        (command,) = result.data["modules"][0]["commands"]
        assert command["required"] == [
            {"name": "zeta", "type": "integer", "description": "Last."}
        ]
        assert command["optional"] == [
            {"name": "alpha", "type": "boolean", "description": "First."},
            {"name": "beta", "type": "string", "description": "Second."},
        ]

    def test_disabled_and_third_party_flags(self, tmp_path):
        registry = Registry.from_classes(
            [make_module()], third_party=frozenset({"demo_sensor"})
        )
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=False, retention=500)}
        )
        result = self.run_describe(tmp_path, registry, config)
        assert result.data["modules"][0]["enabled"] is False
        assert result.data["modules"][0]["third_party"] is True

    def test_skill_present(self, tmp_path, monkeypatch):
        pkg = make_pkg(tmp_path, monkeypatch, "fakepkg_present", {"SKILL.md": "# Demo skill\n"})
        module_cls = make_module()
        module_cls.__module__ = pkg
        registry = Registry.from_classes([module_cls])
        result = self.run_describe(tmp_path, registry, make_config(tmp_path))
        assert result.data["modules"][0]["skill"] == "# Demo skill\n"
        assert list(result.warnings) == []

    def test_skill_oversized(self, tmp_path, monkeypatch):
        pkg = make_pkg(
            tmp_path, monkeypatch, "fakepkg_oversized", {"SKILL.md": "x" * (32 * 1024 + 1)}
        )
        module_cls = make_module()
        module_cls.__module__ = pkg
        registry = Registry.from_classes([module_cls])
        result = self.run_describe(tmp_path, registry, make_config(tmp_path))
        assert result.data["modules"][0]["skill"] is None
        assert list(result.warnings) == ["SKILL.md for demo_sensor is too large"]

    def test_skill_file_name_invalid(self, tmp_path):
        module_cls = make_module(skill_file="../evil.md")
        registry = Registry.from_classes([module_cls])
        result = self.run_describe(tmp_path, registry, make_config(tmp_path))
        assert result.data["modules"][0]["skill"] is None
        assert list(result.warnings) == [
            "SKILL.md file name for demo_sensor is invalid"
        ]

    def test_skill_read_is_bounded(self, tmp_path, monkeypatch):
        import importlib.resources as resources

        # A skill far over the cap must report "too large" from a bounded
        # read: the whole file is never loaded.
        class BigRef:
            def __truediv__(self, other):
                return self

            def is_file(self):
                return True

            def open(self, mode="r", *args, **kwargs):
                assert "b" in mode
                return io.BytesIO(b"y" * (256 * 1024))

            def read_bytes(self):
                raise AssertionError("must not read the whole skill file")

        monkeypatch.setattr(resources, "files", lambda name: BigRef())
        module_cls = make_module()
        module_cls.__module__ = "bounded_skill_probe"
        registry = Registry.from_classes([module_cls])
        result = self.run_describe(tmp_path, registry, make_config(tmp_path))
        assert result.data["modules"][0]["skill"] is None
        assert list(result.warnings) == ["SKILL.md for demo_sensor is too large"]


def doctor_run(tmp_path, registry=None, config=None, files=None, **kwargs):
    registry = registry if registry is not None else Registry.from_classes([])
    config = config if config is not None else make_config(tmp_path)
    commands_dir = tmp_path / "commands.d"
    commands_dir.mkdir(exist_ok=True)
    kwargs.setdefault("sudo_check", lambda: False)
    impl = CoreCommands(
        registry, config, {"HOME": str(tmp_path)}, commands_dir=commands_dir, **kwargs
    )
    return impl.doctor(None, files if files is not None else {})


class TestDoctor:
    def test_fresh_home_findings(self, tmp_path):
        result = doctor_run(tmp_path)
        data = result.data
        assert data["config"] == {
            "path": "~/.config/housesitter/config.json",
            "ok": True,
            "problem": None,
            "exists": False,
        }
        # A fresh install has no secrets yet: not a problem by itself.
        assert data["secrets"]["ok"] is True
        assert data["secrets"]["problem"] is None
        assert data["secrets"]["exists"] is False
        assert data["data_dir"]["exists"] is False
        assert data["data_dir"]["ok"] is True
        assert [item["name"] for item in data["dropins"]["files"]] == [
            "housesitter.describe.json",
            "housesitter.doctor.json",
        ]
        assert all(item["installed"] is False for item in data["dropins"]["files"])
        assert data["sudo"] == {"in_sudo_group": False}
        assert data["modules"] == []
        # Only the two uninstalled drop-ins count.
        assert data["problems"] == 2

    def test_clean_setup(self, tmp_path):
        config_path = tmp_path / ".config" / "housesitter" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}")
        secrets = tmp_path / "secrets"
        secrets.mkdir(mode=0o700)
        (secrets / "unifi_password").write_text("s3cret-line")
        os.chmod(secrets / "unifi_password", 0o600)
        root = tmp_path / ".local" / "share" / "housesitter"
        root.mkdir(parents=True, mode=0o700)
        config = make_config(tmp_path)
        commands_dir = tmp_path / "commands.d"
        commands_dir.mkdir()
        registry = Registry.from_classes([])
        # One matching file (wrong mode) and one file with wrong content.
        files = dropins_mod.generate(registry, "/custom/housesitter")
        dropins_mod.write_files(files, commands_dir)
        os.chmod(commands_dir / "housesitter.describe.json", 0o600)
        (commands_dir / "housesitter.doctor.json").write_text("{}\n")
        (commands_dir / "housesitter.stale.json").write_text("{}\n")
        impl = CoreCommands(
            registry, config, {"HOME": str(tmp_path)},
            commands_dir=commands_dir, sudo_check=lambda: True,
        )
        result = impl.doctor(None, {})
        data = result.data
        assert data["config"] == {
            "path": "~/.config/housesitter/config.json",
            "ok": True,
            "problem": None,
            "exists": True,
        }
        assert data["secrets"]["ok"] is True
        assert data["secrets"]["exists"] is True
        assert data["secrets"]["files"] == [
            {"name": "unifi_password", "ok": True, "problem": None}
        ]
        assert data["data_dir"] == {
            "path": "~/.local/share/housesitter",
            "ok": True,
            "problem": None,
            "exists": True,
        }
        by_name = {item["name"]: item for item in data["dropins"]["files"]}
        assert set(by_name) == {
            "housesitter.describe.json", "housesitter.doctor.json"
        }
        assert by_name["housesitter.describe.json"]["matches"] is True
        assert by_name["housesitter.describe.json"]["problem"] == (
            "installed but not root-owned with mode 644"
        )
        assert by_name["housesitter.doctor.json"]["matches"] is False
        assert by_name["housesitter.doctor.json"]["problem"] == (
            "does not match the generated drop-in"
        )
        assert data["sudo"] == {"in_sudo_group": True}
        assert data["problems"] == 2

    def test_oversized_dropin_file_reported(self, tmp_path):
        commands_dir = tmp_path / "commands.d"
        commands_dir.mkdir(exist_ok=True)
        big = commands_dir / "housesitter.doctor.json"
        big.write_bytes(b"x" * (70 * 1024))
        config = make_config(tmp_path)
        impl = CoreCommands(
            Registry.from_classes([]), config, {"HOME": str(tmp_path)},
            commands_dir=commands_dir, sudo_check=lambda: False,
        )
        result = impl.doctor(None, {})
        by_name = {item["name"]: item for item in result.data["dropins"]["files"]}
        item = by_name["housesitter.doctor.json"]
        assert item["installed"] is True
        assert item["matches"] is False
        assert item["problem"] == "file is larger than 64 KiB"

    def test_oversized_dropin_read_is_bounded(self, tmp_path, monkeypatch):
        commands_dir = tmp_path / "commands.d"
        commands_dir.mkdir(exist_ok=True)
        big = commands_dir / "housesitter.doctor.json"
        big.write_bytes(b"x" * (1024 * 1024))
        read_bytes = []
        real_read = os.read

        def counting_read(fd, n):
            block = real_read(fd, n)
            read_bytes.append(len(block))
            return block

        monkeypatch.setattr(os, "read", counting_read)
        impl = CoreCommands(
            Registry.from_classes([]), make_config(tmp_path), {"HOME": str(tmp_path)},
            commands_dir=commands_dir, sudo_check=lambda: False,
        )
        impl.doctor(None, {})
        assert sum(read_bytes) <= 64 * 1024 + 1

    def test_secrets_dir_mode_refused(self, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir(mode=0o755)
        result = doctor_run(tmp_path)
        assert result.data["secrets"]["problem"] == "secrets directory must have mode 700"

    def test_secret_symlink_refused(self, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir(mode=0o700)
        (tmp_path / "real").write_text("x")
        os.symlink(tmp_path / "real", secrets / "linkname")
        result = doctor_run(tmp_path)
        (item,) = result.data["secrets"]["files"]
        assert item == {
            "name": "linkname", "ok": False, "problem": "must not be a symlink"
        }

    def test_secret_group_readable_refused(self, tmp_path):
        secrets = tmp_path / "secrets"
        secrets.mkdir(mode=0o700)
        target = secrets / "unifi_password"
        target.write_text("s3cret-line")
        os.chmod(target, 0o640)
        result = doctor_run(tmp_path)
        (item,) = result.data["secrets"]["files"]
        assert item["ok"] is False
        assert "group- or world-readable" in item["problem"]

    def test_module_check_results_sanitised_and_capped(self, tmp_path):
        def check(self, ctx):
            return ["first", "y" * 300, ""] + [f"extra{n}" for n in range(25)]

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config,
                            files={"check": True})
        (entry,) = result.data["modules"]
        assert entry["id"] == "demo_sensor"
        assert entry["enabled"] is True
        assert entry["problem"] is None
        assert entry["check"][:2] == ["first", "y" * 200]
        assert len(entry["check"]) == 20
        # Fresh-home findings: only the three uninstalled drop-ins.
        assert result.data["problems"] == 3

    def test_module_check_failure_maps_code_only(self, tmp_path):
        from housesitter import sdk as sdk_mod

        def check(self, ctx):
            raise sdk_mod.CommandError("upstream_auth", "login PLANTED-SECRET-hush")

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config,
                            files={"check": True})
        (entry,) = result.data["modules"]
        assert entry["check"] is None
        assert entry["problem"] == "upstream_auth"
        assert "PLANTED-SECRET-hush" not in json.dumps(result.data)

    def test_disabled_module_check_skipped(self, tmp_path):
        seen = []

        def check(self, ctx):
            seen.append(True)
            return ["x"]

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=False, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config,
                            files={"check": True})
        (entry,) = result.data["modules"]
        assert entry == {
            "id": "demo_sensor", "enabled": False, "check": None, "problem": None
        }
        assert seen == []

    def test_check_not_run_by_default(self, tmp_path):
        seen = []

        def check(self, ctx):
            seen.append(True)
            return ["x"]

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config)
        assert result.data["modules"][0]["check"] is None
        assert seen == []

    def test_module_check_cannot_save(self, tmp_path):
        def check(self, ctx):
            ctx.store.save("snapshot", {"n": 1})
            return ["saved"]

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config,
                            files={"check": True})
        (entry,) = result.data["modules"]
        assert entry["check"] is None
        assert entry["problem"] == "config_invalid"
        root = tmp_path / ".local" / "share" / "housesitter"
        assert not root.exists()

    def test_module_check_can_list_and_load(self, tmp_path):
        from housesitter.core.store import FileStore

        real = FileStore("demo_sensor", env={"HOME": str(tmp_path)})
        saved_id = real.save("snapshot", {"n": 7})

        def check(self, ctx):
            page = ctx.store.list("snapshot")
            loaded = ctx.store.load(saved_id)
            assert loaded["n"] == 7
            return [f"found-{page.total}"]

        module_cls = make_module(check=check)
        registry = Registry.from_classes([module_cls])
        config = make_config(
            tmp_path, {"demo_sensor": ModuleConfig(enabled=True, retention=500)}
        )
        result = doctor_run(tmp_path, registry=registry, config=config,
                            files={"check": True})
        (entry,) = result.data["modules"]
        assert entry["problem"] is None
        assert entry["check"] == ["found-1"]
