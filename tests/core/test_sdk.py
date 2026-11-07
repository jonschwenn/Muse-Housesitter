"""Pin the public module API: every construction-time ValueError, plus wording."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from housesitter import sdk
from housesitter.core import envelope, http


def test_reexports_are_the_core_objects():
    assert sdk.Allow is http.Allow
    assert sdk.NotAllowed is http.NotAllowed
    assert sdk.UpstreamError is http.UpstreamError
    assert sdk.Response is http.Response
    assert sdk.ERROR_CODES is envelope.ERROR_CODES


def test_all_exports_declared():
    for name in sdk.__all__:
        assert getattr(sdk, name, None) is not None, name


class TestCommandError:
    def test_ok(self):
        err = sdk.CommandError("timeout", "the test timed out")
        assert err.code == "timeout"
        assert err.reason == "the test timed out"

    def test_unknown_code(self):
        with pytest.raises(ValueError):
            sdk.CommandError("nope", "whatever")


class TestParam:
    def test_minimal(self):
        param = sdk.Param("string", "Free text.")
        assert param.enum is None
        assert param.default is None

    def test_bad_type(self):
        with pytest.raises(ValueError):
            sdk.Param("date", "When.")

    @pytest.mark.parametrize("description", ["", "   ", None, 42])
    def test_bad_description(self, description):
        with pytest.raises(ValueError):
            sdk.Param("string", description)

    def test_enum_mismatch(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", enum=(1, "two"))

    def test_enum_must_be_a_sequence(self):
        with pytest.raises(ValueError):
            sdk.Param("string", "Name.", enum="abc")

    def test_min_max_on_string(self):
        with pytest.raises(ValueError):
            sdk.Param("string", "Name.", minimum=1)

    def test_max_len_on_integer(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", max_len=10)

    @pytest.mark.parametrize("max_len", [0, -3, True, "10"])
    def test_bad_max_len(self, max_len):
        with pytest.raises(ValueError):
            sdk.Param("string", "Name.", max_len=max_len)

    def test_minimum_above_maximum(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", minimum=10, maximum=5)

    def test_default_wrong_type(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", default="ten")

    def test_default_outside_enum(self):
        with pytest.raises(ValueError):
            sdk.Param("string", "Mode.", enum=("a", "b"), default="c")

    def test_default_outside_range(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", minimum=1, maximum=5, default=9)

    def test_default_too_long(self):
        with pytest.raises(ValueError):
            sdk.Param("string", "Name.", max_len=3, default="toolong")

    def test_bool_is_not_an_integer_default(self):
        with pytest.raises(ValueError):
            sdk.Param("integer", "Count.", default=True)

    def test_bool_is_not_a_number_default(self):
        with pytest.raises(ValueError):
            sdk.Param("number", "Ratio.", default=False)

    def test_integer_accepts_int(self):
        assert sdk.Param("integer", "Count.", default=3).default == 3

    def test_number_accepts_int_and_float(self):
        assert sdk.Param("number", "Ratio.", default=3).default == 3
        assert sdk.Param("number", "Ratio.", default=2.5).default == 2.5

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_number_rejects_nonfinite(self, bad):
        with pytest.raises(ValueError):
            sdk.Param("number", "Ratio.", default=bad)

    def test_drop_in_description_plain(self):
        param = sdk.Param("string", "Free text.")
        assert param.drop_in_description() == "Free text."

    def test_drop_in_description_wording(self):
        param = sdk.Param(
            "integer",
            "How many rows.",
            enum=(10, 50, 100),
            default=50,
            minimum=1,
            maximum=100,
            # max_len is string-only, so a second param covers it below.
        )
        assert param.drop_in_description() == (
            "How many rows. One of: 10, 50, 100. Default 50. "
            "Minimum 1. Maximum 100."
        )

    def test_drop_in_description_max_len(self):
        param = sdk.Param("string", "Label.", max_len=200)
        assert param.drop_in_description() == (
            "Label. Maximum length 200 characters."
        )

    def test_default_is_frozen(self):
        param = sdk.Param("array", "Rows.", default=[1, 2])
        assert param.default == (1, 2)
        assert isinstance(param.default, tuple)
        mapping = sdk.Param("object", "Obj.", default={"a": 1})
        assert isinstance(mapping.default, MappingProxyType)
        with pytest.raises(TypeError):
            mapping.default["b"] = 2


class TestSettingNestedDefaults:
    def test_nested_default_must_match_pattern(self):
        with pytest.raises(ValueError):
            sdk.Setting(
                type="object",
                default={"site": "bad site!"},
                properties={"site": sdk.Setting(type="string", pattern=r"[a-z]+")},
            )

    def test_doubly_nested_default_is_checked(self):
        inner = sdk.Setting(
            type="object", properties={"port": sdk.Setting(type="integer", maximum=10)}
        )
        with pytest.raises(ValueError):
            sdk.Setting(
                type="object", default={"inner": {"port": 99}}, properties={"inner": inner}
            )

    def test_valid_nested_default_accepted(self):
        sdk.Setting(
            type="object",
            default={"site": "default"},
            properties={"site": sdk.Setting(type="string", pattern=r"[a-z]+")},
        )


class TestSetting:
    def test_minimal(self):
        setting = sdk.Setting(type="string")
        assert setting.required is False
        assert setting.default is None

    def test_bad_type(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="array")

    def test_bad_required_flag(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", required="yes")

    def test_required_and_default_are_exclusive(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", required=True, default="x")

    def test_default_wrong_type(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="integer", default="8080")

    def test_enum_mismatch(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", enum=("a", "b"), default="c")

    def test_pattern_on_non_string(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="integer", pattern=r"\d+")

    def test_pattern_must_compile(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", pattern=r"([a-z")

    def test_default_must_match_pattern(self):
        with pytest.raises(ValueError):
            sdk.Setting(
                type="string", pattern=r"[a-z]+", default="ABC"
            )

    def test_min_max_on_string(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", minimum=1)

    def test_min_max_on_boolean(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="boolean", maximum=1)

    def test_minimum_above_maximum(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="integer", minimum=9, maximum=2)

    def test_max_len_on_number(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="number", max_len=4)

    def test_object_needs_properties(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="object")

    def test_properties_forbidden_off_object(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="string", properties={"a": sdk.Setting(type="string")})

    def test_properties_must_be_settings(self):
        with pytest.raises(ValueError):
            sdk.Setting(type="object", properties={"a": "string"})

    def test_properties_are_read_only(self):
        setting = sdk.Setting(
            type="object", properties={"a": sdk.Setting(type="string")}
        )
        assert isinstance(setting.properties, MappingProxyType)
        with pytest.raises(TypeError):
            setting.properties["b"] = sdk.Setting(type="string")

    def test_object_default_unknown_key(self):
        with pytest.raises(ValueError):
            sdk.Setting(
                type="object",
                properties={"a": sdk.Setting(type="string")},
                default={"zzz": "x"},
            )

    def test_tls_setting_shape(self):
        tls = sdk.TLS_SETTING
        assert tls.type == "object"
        assert set(tls.properties) == {
            "ca_file",
            "server_hostname",
            "fingerprint_sha256",
            "insecure",
        }
        assert tls.properties["insecure"].default is False


class TestField:
    @pytest.mark.parametrize(
        "path",
        ["data.aps[].radios[].signal_dbm", "data.aps", "source.flavor", "source.a[].b",
         "data.aps[]"],
    )
    def test_accepted_paths(self, path):
        assert sdk.Field(path=path, type="string").path == path

    @pytest.mark.parametrize(
        "path",
        [
            "data",
            "source",
            "",
            "data.",
            ".data.a",
            "data..a",
            "data.0aps",
            "data.Aps",
            "data.aps[",
            "data.aps[]x",
            "info.x",
            "data.aps-signal",
            "data.aps[0]",
        ],
    )
    def test_rejected_paths(self, path):
        with pytest.raises(ValueError):
            sdk.Field(path=path, type="string")

    def test_bad_type(self):
        with pytest.raises(ValueError):
            sdk.Field(path="data.a", type="date")

    def test_untrusted_only_for_string(self):
        with pytest.raises(ValueError):
            sdk.Field(path="data.a", type="integer", untrusted=True)
        assert sdk.Field(path="data.a", type="string", untrusted=True).untrusted

    @pytest.mark.parametrize("max_len", [0, -1, True, "64"])
    def test_bad_max_len(self, max_len):
        with pytest.raises(ValueError):
            sdk.Field(path="data.a", type="string", max_len=max_len)

    def test_flags_must_be_bool(self):
        with pytest.raises(ValueError):
            sdk.Field(path="data.a", type="string", nullable="yes")


class TestCommand:
    def test_minimal(self):
        command = sdk.Command(
            name="snapshot", handler="snapshot", description="Read things."
        )
        assert command.timeout_ms == 30_000
        assert command.fields == ()
        assert command.schema_version == 1

    @pytest.mark.parametrize("name", ["Snapshot", "9lives", "", "has-dash", "a b"])
    def test_bad_name(self, name):
        with pytest.raises(ValueError):
            sdk.Command(name=name, handler="snapshot", description="Read things.")

    @pytest.mark.parametrize("handler", ["", "not a handler", "a-b", "9lives"])
    def test_bad_handler(self, handler):
        with pytest.raises(ValueError):
            sdk.Command(name="snapshot", handler=handler, description="Read things.")

    @pytest.mark.parametrize("description", ["", "   ", "x" * 1025])
    def test_bad_description(self, description):
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot", handler="snapshot", description=description
            )

    def test_param_overlap(self):
        param = sdk.Param("string", "Label.")
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                required={"label": param},
                optional={"label": param},
            )

    def test_bad_param_name(self):
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                optional={"Has-Dash": sdk.Param("string", "Label.")},
            )

    def test_params_are_read_only(self):
        command = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read things.",
            optional={"label": sdk.Param("string", "Label.")},
        )
        with pytest.raises(TypeError):
            command.optional["other"] = sdk.Param("string", "Other.")

    @pytest.mark.parametrize("timeout_ms", [4999, 600001, 0, -1, True, "30000"])
    def test_bad_timeout(self, timeout_ms):
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                timeout_ms=timeout_ms,
            )

    def test_field_tree_collisions_rejected(self):
        def cmd(*fields):
            return sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                fields=fields,
            )

        # Consistent declarations are accepted.
        cmd(sdk.Field("data.aps", "array"), sdk.Field("data.aps[].x", "string"))
        cmd(sdk.Field("data.x", "object"), sdk.Field("data.x.y", "string"))
        cmd(
            sdk.Field("data.tags", "array"),
            sdk.Field("data.tags[]", "string"),
        )
        cmd(
            sdk.Field("data.aps[]", "object"),
            sdk.Field("data.aps[].x", "string"),
        )
        # A segment used both as x[] and as a non-array leaf x.
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.aps", "string"),
                sdk.Field("data.aps[].x", "string"),
            )
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.aps", "object"),
                sdk.Field("data.aps[].x", "string"),
            )
        # A non-object leaf with child fields.
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.x", "string"),
                sdk.Field("data.x.y", "string"),
            )
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.x", "array"),
                sdk.Field("data.x.y", "string"),
            )
        # Item leaves with children must be objects.
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.a[]", "string"),
                sdk.Field("data.a[].b", "string"),
            )
        # A segment used both as a list and as an object.
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.a[]", "string"),
                sdk.Field("data.a.b", "string"),
            )
        with pytest.raises(ValueError):
            cmd(
                sdk.Field("data.a.b", "string"),
                sdk.Field("data.a[].c", "string"),
            )

    def test_opaque_object_leaf_rejected(self):
        # An object field must declare at least one descendant: undeclared
        # keys would pass upstream-controlled text and identifiers through.
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                fields=(sdk.Field("data.meta", "object"),),
            )
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                fields=(sdk.Field("source.meta", "object"),),
            )

    def test_object_with_declared_children_accepted(self):
        cmd = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read things.",
            fields=(
                sdk.Field("data.meta", "object"),
                sdk.Field("data.meta.inner", "integer"),
            ),
        )
        assert [item.path for item in cmd.fields] == [
            "data.meta", "data.meta.inner",
        ]

    def test_opaque_array_leaf_still_accepted(self):
        cmd = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read things.",
            fields=(sdk.Field("data.tags", "array"),),
        )
        assert [item.path for item in cmd.fields] == ["data.tags"]

    def test_fields_stored_as_tuple_and_unique(self):
        first = sdk.Field(path="data.a", type="string")
        command = sdk.Command(
            name="snapshot",
            handler="snapshot",
            description="Read things.",
            fields=[first],
        )
        assert command.fields == (first,)
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                fields=[first, sdk.Field(path="data.a", type="integer")],
            )

    @pytest.mark.parametrize("schema_version", [0, -1, True, "1"])
    def test_bad_schema_version(self, schema_version):
        with pytest.raises(ValueError):
            sdk.Command(
                name="snapshot",
                handler="snapshot",
                description="Read things.",
                schema_version=schema_version,
            )


class TestPaging:
    def test_ok(self):
        paging = sdk.Paging(
            command="get",
            lists=["clients", "aps"],
            params={"kind": "full", "id": "snapshot_20261104T151500Z_0001"},
            start={"clients": 120},
        )
        assert paging.lists == ("clients", "aps")

    @pytest.mark.parametrize("key", ["section", "offset", "remaining"])
    def test_reserved_params_keys_rejected(self, key):
        # The runner sets section/offset and keeps remaining beside params.
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=["a"], params={key: "x"})

    def test_id_params_key_still_allowed(self):
        paging = sdk.Paging(
            command="get", lists=["a"], params={"id": "snapshot_1"}
        )
        assert dict(paging.params) == {"id": "snapshot_1"}

    def test_empty_lists(self):
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=[])

    def test_duplicate_lists(self):
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=["a", "a"])

    def test_bad_list_name(self):
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=["Has-Dash"])

    def test_start_key_must_be_listed(self):
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=["a"], start={"b": 0})

    @pytest.mark.parametrize("offset", [-1, True, "0"])
    def test_bad_start_offset(self, offset):
        with pytest.raises(ValueError):
            sdk.Paging(command="get", lists=["a"], start={"a": offset})

    @pytest.mark.parametrize(
        "command", ["  ", "", "Get", "housesitter.demo.get", "get page", "1get"]
    )
    def test_command_must_be_a_command_name(self, command):
        with pytest.raises(ValueError):
            sdk.Paging(command=command, lists=["a"])


class TestResult:
    def test_minimal(self):
        result = sdk.Result(data={"aps": []})
        assert result.source == {}
        assert result.warnings == ()
        assert result.collected_at is None

    def test_full(self):
        stamp = datetime.now(timezone.utc)
        result = sdk.Result(
            data={"a": 1},
            source={"kind": "demo"},
            paging=sdk.Paging(command="get", lists=["a"]),
            warnings=["note"],
            label="before",
            collected_at=stamp,
            save="snapshot",
        )
        assert result.collected_at == stamp

    def test_data_must_be_a_mapping(self):
        with pytest.raises(ValueError):
            sdk.Result(data=[1, 2])

    def test_naive_collected_at_rejected(self):
        with pytest.raises(ValueError):
            sdk.Result(data={}, collected_at=datetime(2026, 1, 1))

    def test_collected_at_may_be_non_utc_if_aware(self):
        zone = timezone(timedelta(hours=-8))
        stamp = datetime(2026, 1, 1, tzinfo=zone)
        assert sdk.Result(data={}, collected_at=stamp).collected_at == stamp

    @pytest.mark.parametrize("save", ["", "Snapshot", "a b", "x" * 33, 42])
    def test_bad_save_kind(self, save):
        with pytest.raises(ValueError):
            sdk.Result(data={}, save=save)

    def test_warnings_must_be_strings(self):
        with pytest.raises(ValueError):
            sdk.Result(data={}, warnings=["ok", 42])

    def test_warnings_stored_as_tuple(self):
        assert sdk.Result(data={}, warnings=["a"]).warnings == ("a",)


class TestModule:
    def test_check_defaults_to_empty(self):
        assert sdk.Module().check(ctx=None) == []

    def test_skill_file_default(self):
        assert sdk.Module.skill_file == "SKILL.md"


class TestStoredItem:
    def test_valid(self):
        item = sdk.StoredItem(
            id="snapshot_20261104T151500Z_3fa1",
            kind="snapshot",
            collected_at="2026-11-04T15:15:00Z",
            label="before",
        )
        assert item.kind == "snapshot"

    def test_optional_fields_default_to_none(self):
        item = sdk.StoredItem(id="snapshot_20261104T151500Z_3fa1", kind="snapshot")
        assert item.collected_at is None
        assert item.label is None

    @pytest.mark.parametrize(
        "bad_id",
        [
            "",
            "snapshot",
            "Snapshot_20261104T151500Z_3fa1",
            "snapshot_20261104T151500Z_ZZZZ",
            "snapshot_20261104T151500Z_3fa",
            "snapshot_20261104151500Z_3fa1",
            "snapshot_20261104T151500Z_3fa1.json",
            "../snapshot_20261104T151500Z_3fa1",
        ],
    )
    def test_bad_id_rejected(self, bad_id):
        with pytest.raises(ValueError):
            sdk.StoredItem(id=bad_id, kind="snapshot")

    @pytest.mark.parametrize("bad_kind", ["", "Snapshot", "a" * 33, "a-b"])
    def test_bad_kind_rejected(self, bad_kind):
        with pytest.raises(ValueError):
            sdk.StoredItem(id="snapshot_20261104T151500Z_3fa1", kind=bad_kind)

    def test_kind_must_match_id_prefix(self):
        with pytest.raises(ValueError):
            sdk.StoredItem(id="snapshot_20261104T151500Z_3fa1", kind="other")
        with pytest.raises(ValueError):
            sdk.StoredItem(id="snapshot_extra_20261104T151500Z_3fa1", kind="snapshot")

    def test_long_collected_at_rejected(self):
        with pytest.raises(ValueError):
            sdk.StoredItem(
                id="snapshot_20261104T151500Z_3fa1",
                kind="snapshot",
                collected_at="x" * 33,
            )

    def test_long_label_rejected(self):
        with pytest.raises(ValueError):
            sdk.StoredItem(
                id="snapshot_20261104T151500Z_3fa1",
                kind="snapshot",
                label="x" * 201,
            )
        sdk.StoredItem(
            id="snapshot_20261104T151500Z_3fa1", kind="snapshot", label="x" * 200
        )

    def test_non_string_fields_rejected(self):
        with pytest.raises(ValueError):
            sdk.StoredItem(
                id="snapshot_20261104T151500Z_3fa1", kind="snapshot", label=42
            )

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-date",
            "2026-11-04 15:15:00",
            "2026-11-04T15:15:00",
            "2026-11-04T15:15:00+00:00",
            "2026-11-04T15:15:00Z ",
            " 2026-11-04T15:15:00Z",
            "",
        ],
    )
    def test_bad_collected_at_format_rejected(self, bad):
        with pytest.raises(ValueError):
            sdk.StoredItem(
                id="snapshot_20261104T151500Z_3fa1",
                kind="snapshot",
                collected_at=bad,
            )

    def test_good_collected_at_accepted(self):
        item = sdk.StoredItem(
            id="snapshot_20261104T151500Z_3fa1",
            kind="snapshot",
            collected_at="2026-11-04T15:15:00Z",
        )
        assert item.collected_at == "2026-11-04T15:15:00Z"


def test_drop_in_description_writes_values_as_json():
    from housesitter.sdk import Param

    assert Param("boolean", "Flag.", default=False).drop_in_description() == (
        "Flag. Default false."
    )
    assert Param("string", "Pick.", enum=("a", "b"), default="a").drop_in_description() == (
        "Pick. One of: a, b. Default a."
    )
    assert Param("array", "List.", default=[1, 2]).drop_in_description() == (
        "List. Default [1,2]."
    )
