"""Pin config loading and validation: defaults, errors, and no value leaks."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from housesitter import sdk
from housesitter.core import config as config_mod
from housesitter.core.config import ConfigError, load_config
from housesitter.core.registry import Registry


class DemoSensor(sdk.Module):
    id = "demo_sensor"
    title = "Demo sensor"
    version = "0.1.0"
    config = {
        "host": sdk.Setting(type="string", required=True),
        "port": sdk.Setting(type="integer", default=8080, minimum=1, maximum=65535),
        "site": sdk.Setting(
            type="string", default="default", pattern=r"[A-Za-z0-9_-]{1,64}"
        ),
        "mode": sdk.Setting(type="string", default="auto", enum=("auto", "fast")),
        "tls": sdk.TLS_SETTING,
    }
    http_allow = (sdk.Allow("GET", r"/api/status-example"),)
    commands = (
        sdk.Command(
            name="snapshot", handler="snapshot", description="Read demo sensors."
        ),
    )

    def snapshot(self, ctx, params):
        return sdk.Result(data={})


@pytest.fixture()
def registry():
    return Registry.from_classes([DemoSensor])


def write_config(tmp_path, obj):
    target = tmp_path / "config.json"
    target.write_text(json.dumps(obj))
    return target


class TestConfigError:
    def test_str_is_key_problem(self):
        err = ConfigError("modules.demo_sensor.site", "required setting is missing")
        assert str(err) == "modules.demo_sensor.site: required setting is missing"
        assert err.key == "modules.demo_sensor.site"
        assert err.problem == "required setting is missing"

    def test_code_is_config_invalid(self):
        assert ConfigError("k", "p").code == "config_invalid"


class TestMissingFile:
    def test_defaults_with_no_modules(self, tmp_path, registry):
        config = load_config(registry, path=tmp_path / "missing.json")
        assert config.identifiers == "full"
        assert config.retention == 500
        assert config.modules == {}
        fallback = config.module("demo_sensor")
        assert fallback.enabled is False
        assert fallback.retention == 500
        assert fallback.settings == {}

    def test_secrets_dir_derives_from_file(self, tmp_path, registry):
        config = load_config(registry, path=tmp_path / "sub" / "missing.json")
        assert config.secrets_dir == tmp_path / "sub" / "secrets"


class TestUnreadableFiles:
    def test_directory_is_not_a_file(self, tmp_path, registry):
        with pytest.raises(ConfigError):
            load_config(registry, path=tmp_path)

    def test_bad_json(self, tmp_path, registry):
        target = tmp_path / "config.json"
        target.write_text("{not json, PLANTED-SECRET-x}")
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry, path=target)
        assert "PLANTED-SECRET" not in str(excinfo.value)

    def test_top_level_must_be_an_object(self, tmp_path, registry):
        target = write_config(tmp_path, [1, 2])
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    def test_oversized_file(self, tmp_path, registry):
        target = tmp_path / "config.json"
        target.write_bytes(b" " * (1024 * 1024 + 1))
        with pytest.raises(ConfigError):
            load_config(registry, path=target)


class TestTopLevelKeys:
    def test_unknown_top_key(self, tmp_path, registry):
        target = write_config(tmp_path, {"frobnicate": {}})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    def test_unknown_core_key(self, tmp_path, registry):
        target = write_config(tmp_path, {"housesitter": {"frobnicate": 1}})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    @pytest.mark.parametrize("identifiers", ["none", "full"])
    def test_identifiers_ok(self, tmp_path, registry, identifiers):
        target = write_config(tmp_path, {"housesitter": {"identifiers": identifiers}})
        assert load_config(registry, path=target).identifiers == identifiers

    def test_identifiers_bad(self, tmp_path, registry):
        target = write_config(tmp_path, {"housesitter": {"identifiers": "some"}})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    @pytest.mark.parametrize("retention", [-1, True, "500", 1.5])
    def test_retention_bad(self, tmp_path, registry, retention):
        target = write_config(tmp_path, {"housesitter": {"retention": retention}})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    def test_core_section_must_be_an_object(self, tmp_path, registry):
        target = write_config(tmp_path, {"housesitter": ["full"]})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)


class TestModuleSections:
    def test_unknown_module(self, tmp_path, registry):
        target = write_config(tmp_path, {"modules": {"nope_sensor": {}}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry, path=target)
        assert excinfo.value.key == "modules.nope_sensor"

    def test_section_must_be_an_object(self, tmp_path, registry):
        target = write_config(tmp_path, {"modules": {"demo_sensor": ["x"]}})
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    def test_unknown_module_key(self, tmp_path, registry):
        target = write_config(
            tmp_path,
            {"modules": {"demo_sensor": {"host": "192.0.2.1", "frobnicate": 1}}},
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry, path=target)
        assert excinfo.value.key == "modules.demo_sensor.frobnicate"

    def test_required_missing(self, tmp_path, registry):
        target = write_config(tmp_path, {"modules": {"demo_sensor": {}}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry, path=target)
        assert excinfo.value.key == "modules.demo_sensor.host"

    def test_pattern_mismatch_never_echoes_value(self, tmp_path, registry):
        bad = "bad site !! PLANTED-SECRET-9"
        target = write_config(
            tmp_path, {"modules": {"demo_sensor": {"host": "192.0.2.1", "site": bad}}}
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry, path=target)
        assert excinfo.value.key == "modules.demo_sensor.site"
        assert "PLANTED-SECRET" not in str(excinfo.value)

    def test_disabled_module_ignores_extra_keys(self, tmp_path, registry):
        target = write_config(
            tmp_path,
            {
                "modules": {
                    "demo_sensor": {
                        "enabled": False,
                        "frobnicate": "anything",
                        "host": 42,
                    }
                }
            },
        )
        config = load_config(registry, path=target)
        assert config.modules["demo_sensor"].enabled is False

    def test_enabled_defaults_to_true_and_applies_defaults(self, tmp_path, registry):
        target = write_config(
            tmp_path, {"modules": {"demo_sensor": {"host": "192.0.2.1"}}}
        )
        config = load_config(registry, path=target)
        module = config.modules["demo_sensor"]
        assert module.enabled is True
        assert module.retention == 500
        assert module.settings["host"] == "192.0.2.1"
        assert module.settings["port"] == 8080
        assert module.settings["site"] == "default"
        assert module.settings["mode"] == "auto"
        assert module.settings["tls"] == {
            "ca_file": None,
            "server_hostname": None,
            "fingerprint_sha256": None,
            "insecure": False,
        }
        assert isinstance(module.settings, MappingProxyType)

    def test_enabled_must_be_a_boolean(self, tmp_path, registry):
        target = write_config(
            tmp_path, {"modules": {"demo_sensor": {"enabled": "yes"}}}
        )
        with pytest.raises(ConfigError):
            load_config(registry, path=target)

    def test_module_retention_override(self, tmp_path, registry):
        target = write_config(
            tmp_path,
            {
                "housesitter": {"retention": 10},
                "modules": {"demo_sensor": {"host": "192.0.2.1", "retention": 3}},
            },
        )
        config = load_config(registry, path=target)
        assert config.retention == 10
        assert config.modules["demo_sensor"].retention == 3

    def test_module_keeps_core_retention_by_default(self, tmp_path, registry):
        target = write_config(
            tmp_path,
            {
                "housesitter": {"retention": 10},
                "modules": {"demo_sensor": {"host": "192.0.2.1"}},
            },
        )
        assert load_config(registry, path=target).modules["demo_sensor"].retention == 10

    def test_unregistered_module_has_no_section(self, tmp_path, registry):
        target = write_config(tmp_path, {})
        config = load_config(registry, path=target)
        assert "demo_sensor" not in config.modules
        assert config.module("demo_sensor").enabled is False


class TestPaths:
    def test_config_path_override(self, tmp_path):
        env = {"HOUSESITTER_CONFIG": str(tmp_path / "custom.json")}
        assert config_mod.config_path(env) == tmp_path / "custom.json"

    def test_config_path_default_from_home(self):
        env = {"HOME": "/home/owner"}
        assert config_mod.config_path(env) == Path(
            "/home/owner/.config/housesitter/config.json"
        )

    def test_load_uses_override(self, tmp_path, registry):
        custom = tmp_path / "custom.json"
        custom.write_text(json.dumps({"modules": {"demo_sensor": {"enabled": False}}}))
        config = load_config(registry, env={"HOUSESITTER_CONFIG": str(custom)})
        assert config.path == custom
        assert config.secrets_dir == tmp_path / "secrets"
        assert config.modules["demo_sensor"].enabled is False


class TestValidateSetting:
    def test_none_with_default(self):
        setting = sdk.Setting(type="integer", default=8080)
        assert config_mod.validate_setting(setting, None, "k") == 8080

    def test_none_without_default_is_null(self):
        setting = sdk.Setting(type="string")
        assert config_mod.validate_setting(setting, None, "k") is None

    def test_none_when_required(self):
        setting = sdk.Setting(type="string", required=True)
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, None, "k")

    def test_tls_defaults_applied_recursively(self):
        resolved = config_mod.validate_setting(sdk.TLS_SETTING, None, "tls")
        assert resolved == {
            "ca_file": None,
            "server_hostname": None,
            "fingerprint_sha256": None,
            "insecure": False,
        }

    def test_object_defaults_fill_missing_keys(self):
        setting = sdk.Setting(
            type="object",
            properties={
                "a": sdk.Setting(type="string", default="x"),
                "b": sdk.Setting(type="string"),
            },
        )
        assert config_mod.validate_setting(setting, {}, "k") == {"a": "x", "b": None}

    def test_object_unknown_key(self):
        setting = sdk.Setting(
            type="object", properties={"a": sdk.Setting(type="string")}
        )
        with pytest.raises(ConfigError) as excinfo:
            config_mod.validate_setting(setting, {"zzz": "x"}, "k")
        assert excinfo.value.key == "k.zzz"

    def test_nested_required_missing(self):
        setting = sdk.Setting(
            type="object",
            properties={"auth": sdk.Setting(type="string", required=True)},
        )
        with pytest.raises(ConfigError) as excinfo:
            config_mod.validate_setting(setting, {}, "k")
        assert excinfo.value.key == "k.auth"

    def test_bool_is_not_an_integer(self):
        setting = sdk.Setting(type="integer")
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, True, "k")

    def test_number_accepts_int(self):
        setting = sdk.Setting(type="number")
        assert config_mod.validate_setting(setting, 3, "k") == 3

    def test_number_rejects_nan(self):
        setting = sdk.Setting(type="number")
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, float("nan"), "k")

    def test_enum_rejected(self):
        setting = sdk.Setting(type="string", enum=("a", "b"))
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, "c", "k")

    def test_min_max_rejected(self):
        setting = sdk.Setting(type="integer", minimum=1, maximum=5)
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, 0, "k")
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, 6, "k")

    def test_max_len_rejected(self):
        setting = sdk.Setting(type="string", max_len=3)
        with pytest.raises(ConfigError):
            config_mod.validate_setting(setting, "toolong", "k")

    def test_bad_value_never_echoed(self):
        setting = sdk.Setting(type="integer")
        bad = "oops-PLANTED-SECRET-3"
        with pytest.raises(ConfigError) as excinfo:
            config_mod.validate_setting(setting, bad, "k")
        assert "PLANTED-SECRET" not in str(excinfo.value)
