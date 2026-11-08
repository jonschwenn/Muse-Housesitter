"""Pin module discovery and the command table: validation and collisions."""

from __future__ import annotations

import sys
import types

import pytest

from housesitter import sdk
from housesitter.core import registry as registry_mod
from housesitter.core.registry import (
    Registry,
    RegistryError,
    load_registry,
    validate_module_class,
)


def make_module(name="demo_sensor", **overrides):
    attrs = {
        "id": name,
        "title": "Demo sensor",
        "version": "0.1.0",
        "config": {},
        "http_allow": (sdk.Allow("GET", r"/api/status-example"),),
        "commands": (
            sdk.Command(
                name="snapshot", handler="snapshot", description="Read demo sensors."
            ),
        ),
        "snapshot": lambda self, ctx, params: sdk.Result(data={}),
    }
    attrs.update(overrides)
    return type(f"Module_{name}", (sdk.Module,), attrs)


@pytest.fixture()
def alpha():
    return make_module("alpha_sensor")


@pytest.fixture()
def beta():
    return make_module("beta_sensor")


class TestFromClasses:
    def test_modules_ordered_by_id(self, alpha, beta):
        registry = Registry.from_classes([beta, alpha])
        assert [entry.id for entry in registry.modules] == [
            "alpha_sensor",
            "beta_sensor",
        ]

    def test_command_table(self, alpha):
        registry = Registry.from_classes([alpha])
        full = "housesitter.alpha_sensor.snapshot"
        assert list(registry.commands) == [full]
        registered = registry.get_command(full)
        assert registered.module_id == "alpha_sensor"
        assert registered.command.name == "snapshot"

    def test_get_module(self, alpha):
        registry = Registry.from_classes([alpha])
        assert registry.get_module("alpha_sensor").cls is alpha
        with pytest.raises(KeyError):
            registry.get_module("nope")

    def test_get_command_unknown(self, alpha):
        registry = Registry.from_classes([alpha])
        with pytest.raises(KeyError):
            registry.get_command("housesitter.alpha_sensor.nope")

    def test_duplicate_ids(self, alpha):
        with pytest.raises(RegistryError):
            Registry.from_classes([alpha, make_module("alpha_sensor")])

    def test_third_party_flag(self, alpha):
        registry = Registry.from_classes([alpha], third_party=frozenset({"alpha_sensor"}))
        assert registry.get_module("alpha_sensor").third_party is True
        plain = Registry.from_classes([alpha])
        assert plain.get_module("alpha_sensor").third_party is False


class TestValidateModuleClass:
    def test_not_a_module(self):
        with pytest.raises(RegistryError):
            validate_module_class(object)

    @pytest.mark.parametrize("bad_id", ["", "9lives", "Has-Dash", "a b", 42, None])
    def test_bad_id(self, bad_id):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", id=bad_id))

    def test_empty_title(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", title="  "))

    @pytest.mark.parametrize("version", ["", "1.0", "v1.0.0", "1.0.0-"])
    def test_bad_version(self, version):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", version=version))

    def test_config_must_be_a_mapping(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", config=[("a", 1)]))

    def test_config_must_hold_settings(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", config={"a": "string"}))

    @pytest.mark.parametrize("reserved", ["enabled", "retention"])
    def test_reserved_config_key(self, reserved):
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module("demo", config={reserved: sdk.Setting(type="string")})
            )

    def test_empty_allow_list_is_allowed(self):
        # A module with no network access declares no allow-list entries.
        validate_module_class(make_module("demo", http_allow=()))

    def test_allow_list_must_be_a_list(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", http_allow="GET /"))

    def test_allow_entry_must_be_allow(self):
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module("demo", http_allow=("GET /api/status-example",))
            )

    @pytest.mark.parametrize("method", ["PUT", "DELETE", "POSTMAN", "OPTIONS"])
    def test_write_methods_refused(self, method):
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module(
                    "demo", http_allow=(sdk.Allow(method, r"/api/status-example"),)
                )
            )

    @pytest.mark.parametrize(
        "pattern",
        [
            r"/api/data/submit",
            r"/api/data/submit-login",
            r"/api/login-then-write",
            r"/api/login/extra",
            r"/api/auth/(login|logout)",
            r"/api/.*/login",
            r"/api/s/[a-z]+/login",
            r"login",
        ],
    )
    def test_post_must_be_literal_login_or_logout(self, pattern):
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module("demo", http_allow=(sdk.Allow("POST", pattern),))
            )

    @pytest.mark.parametrize(
        "pattern", [r"/api/auth/login", r"/api/auth/logout", r"/api/login", r"/logout"]
    )
    def test_post_login_ok(self, pattern):
        validate_module_class(
            make_module("demo", http_allow=(sdk.Allow("POST", pattern),))
        )

    def test_no_commands(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", commands=()))

    def test_duplicate_command_names(self):
        first = sdk.Command(
            name="snapshot", handler="snapshot", description="Read demo sensors."
        )
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module("demo", commands=(first, first))
            )

    def test_missing_handler(self):
        with pytest.raises(RegistryError):
            validate_module_class(
                make_module(
                    "demo",
                    commands=(
                        sdk.Command(
                            name="snapshot",
                            handler="missing",
                            description="Read demo sensors.",
                        ),
                    ),
                )
            )

    def test_non_callable_handler(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("demo", snapshot="not-callable"))

    def test_overlong_full_name(self):
        long_id = "a" * 28
        command = sdk.Command(
            name="b" * 28, handler="snapshot", description="Read long things."
        )
        with pytest.raises(RegistryError):
            validate_module_class(make_module(long_id, commands=(command,)))

    def test_expected_id_mismatch(self, alpha):
        with pytest.raises(RegistryError):
            validate_module_class(alpha, expected_id="other_sensor")

    def test_expected_id_match(self, alpha):
        validate_module_class(alpha, expected_id="alpha_sensor")


class FakeEntryPoint:
    def __init__(self, name, loaded=None, error=None):
        self.name = name
        self._loaded = loaded
        self._error = error

    def load(self):
        if self._error is not None:
            raise self._error
        return self._loaded


class FakeSelect:
    def __init__(self, points):
        self._points = points

    def select(self, group=None):
        assert group == "housesitter.modules"
        return self._points


def patch_dict_api(monkeypatch, points):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda: {"housesitter.modules": points}
    )


def patch_select_api(monkeypatch, points):
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda: FakeSelect(points)
    )


class TestEntryPoints:
    def test_dict_api_discovery(self, monkeypatch, alpha):
        patch_dict_api(monkeypatch, [FakeEntryPoint("alpha_sensor", alpha)])
        assert registry_mod._discover_third_party_classes() == [("alpha_sensor", alpha)]

    def test_select_api_discovery(self, monkeypatch, alpha):
        patch_select_api(monkeypatch, [FakeEntryPoint("alpha_sensor", alpha)])
        assert registry_mod._discover_third_party_classes() == [("alpha_sensor", alpha)]

    def test_load_failure_names_entry_point(self, monkeypatch):
        patch_dict_api(
            monkeypatch, [FakeEntryPoint("oops", error=ValueError("boom went wrong"))]
        )
        with pytest.raises(RegistryError) as excinfo:
            registry_mod._discover_third_party_classes()
        assert "oops" in str(excinfo.value)
        assert "boom" not in str(excinfo.value)

    def test_id_mismatch(self, monkeypatch, alpha):
        patch_select_api(monkeypatch, [FakeEntryPoint("other_sensor", alpha)])
        with pytest.raises(RegistryError):
            registry_mod._discover_third_party_classes()

    def test_non_module_refused(self, monkeypatch):
        patch_dict_api(monkeypatch, [FakeEntryPoint("thing", object)])
        with pytest.raises(RegistryError):
            registry_mod._discover_third_party_classes()

    def test_load_registry_marks_third_party(self, monkeypatch, alpha):
        patch_dict_api(monkeypatch, [FakeEntryPoint("alpha_sensor", alpha)])
        registry = load_registry()
        assert registry.get_module("alpha_sensor").third_party is True
        for entry in registry.modules:
            if entry.id != "alpha_sensor":
                assert entry.third_party is False

    def test_load_registry_empty(self, monkeypatch):
        patch_dict_api(monkeypatch, [])
        registry = load_registry()
        assert all(entry.third_party is False for entry in registry.modules)


class FakeModuleInfo:
    def __init__(self, name):
        self.name = name


def install_fake_package(monkeypatch, name, module_obj):
    import pkgutil

    monkeypatch.setitem(sys.modules, f"housesitter.modules.{name}", module_obj)
    monkeypatch.setattr(
        pkgutil, "iter_modules", lambda path: [FakeModuleInfo(name)]
    )


class TestBuiltinDiscovery:
    def test_discovers_one_module(self, monkeypatch):
        package = types.ModuleType("housesitter.modules.fakedemo")
        package.FakeDemo = make_module("fakedemo")
        package.FakeDemo.__module__ = "housesitter.modules.fakedemo"
        install_fake_package(monkeypatch, "fakedemo", package)
        found = registry_mod._discover_builtin_classes()
        assert [cls.id for cls in found] == ["fakedemo"]

    def test_import_failure_names_package(self, monkeypatch):
        import pkgutil

        monkeypatch.setattr(
            pkgutil, "iter_modules", lambda path: [FakeModuleInfo("broken")]
        )
        with pytest.raises(RegistryError) as excinfo:
            registry_mod._discover_builtin_classes()
        assert "broken" in str(excinfo.value)

    def test_zero_or_two_modules_refused(self, monkeypatch):
        package = types.ModuleType("housesitter.modules.empty")
        install_fake_package(monkeypatch, "empty", package)
        with pytest.raises(RegistryError):
            registry_mod._discover_builtin_classes()

    def test_id_must_match_package(self, monkeypatch):
        package = types.ModuleType("housesitter.modules.fakedemo")
        package.FakeDemo = make_module("other_id")
        package.FakeDemo.__module__ = "housesitter.modules.fakedemo"
        install_fake_package(monkeypatch, "fakedemo", package)
        with pytest.raises(RegistryError):
            registry_mod._discover_builtin_classes()


class TestReservedId:
    def test_housesitter_id_is_reserved(self):
        with pytest.raises(RegistryError):
            validate_module_class(make_module("housesitter"))

    def test_from_classes_rejects_housesitter(self):
        with pytest.raises(RegistryError):
            Registry.from_classes([make_module("housesitter")])
