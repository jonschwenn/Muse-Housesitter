"""Pin the concrete handler context: config freeze, secrets, log, HTTP."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from housesitter import sdk
from housesitter.core import context as context_mod
from housesitter.core import http as http_core

from .helpers import Canned, ScriptedServer, canned_handler


class DemoModule(sdk.Module):
    id = "demo_sensor"
    title = "Demo sensor"
    version = "0.1.0"
    config = {
        "host": sdk.Setting(type="string", required=True),
        "port": sdk.Setting(type="integer", default=8080),
    }
    http_allow = (sdk.Allow("GET", r"/api/status-example"),)
    commands = (
        sdk.Command(
            name="snapshot", handler="snapshot", description="Read demo sensors."
        ),
    )

    def snapshot(self, ctx, params):
        return sdk.Result(data={})


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.calls = []

    def request(self, method, target, headers=None, body=None, deadline_s=None):
        self.calls.append(
            {
                "method": method,
                "target": target,
                "headers": headers,
                "body": body,
                "deadline_s": deadline_s,
            }
        )
        return http_core.Response(status=200, headers={}, body=b'{"a":1}')

    def clear_auth(self):
        self.calls.append({"clear_auth": True})

    def close(self):
        self.calls.append({"close": True})


def make_factory(made):
    def factory(scheme, host, port, *, allow, tls, default_timeout_s):
        made["scheme"] = scheme
        made["host"] = host
        made["port"] = port
        made["allow"] = allow
        made["tls"] = tls
        made["default_timeout_s"] = default_timeout_s
        client = FakeClient()
        made["client"] = client
        return client

    return factory


def make_ctx(module=None, config=None, secrets_dir=None, **kwargs):
    module = module or DemoModule()
    kwargs.setdefault("deadline_monotonic", time.monotonic() + 60)
    return context_mod.ModuleContext(
        module,
        {"nested": {"a": 1}, "items": [1, 2]} if config is None else config,
        secrets_dir=secrets_dir or "/nonexistent",
        **kwargs,
    )


def write_secret(tmp_path, name, value, mode=0o600):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    target = secrets_dir / name
    target.write_text(value)
    target.chmod(mode)
    return secrets_dir


class TestConfigFrozen:
    def test_deep_read_only(self):
        ctx = make_ctx()
        assert isinstance(ctx.config, MappingProxyType)
        with pytest.raises(TypeError):
            ctx.config["nested"] = {}
        with pytest.raises(TypeError):
            ctx.config["nested"]["a"] = 2
        with pytest.raises(AttributeError):
            ctx.config["items"].append(3)
        assert ctx.config["nested"]["a"] == 1
        assert ctx.config["items"] == (1, 2)


class TestDeadline:
    def test_future_deadline_is_positive(self):
        ctx = make_ctx(deadline_monotonic=time.monotonic() + 50)
        assert 0.0 < ctx.deadline <= 50.0

    def test_past_deadline_floors_at_zero(self):
        ctx = make_ctx(deadline_monotonic=time.monotonic() - 5)
        assert ctx.deadline == 0.0


class TestNow:
    def test_default_is_aware_utc(self):
        value = make_ctx().now()
        assert value.tzinfo is not None
        assert value.utcoffset() == timedelta(0)

    def test_injected_aware_time_is_returned_in_utc(self):
        zone = timezone(timedelta(hours=-8))
        stamp = datetime(2026, 5, 1, 12, 0, tzinfo=zone)
        ctx = make_ctx(now=lambda: stamp)
        assert ctx.now() == stamp.astimezone(timezone.utc)

    def test_naive_time_is_refused(self):
        ctx = make_ctx(now=lambda: datetime(2026, 5, 1, 12, 0))
        with pytest.raises(ValueError):
            ctx.now()


class TestSecret:
    def test_read_ok(self, tmp_path):
        secrets_dir = write_secret(tmp_path, "demo_token", "opaque-value-42")
        ctx = make_ctx(secrets_dir=secrets_dir)
        assert ctx.secret("demo_token") == "opaque-value-42"

    def test_group_readable_refused_without_value_or_context(self, tmp_path):
        planted = "hidden-PLANTED-SECRET-7"
        secrets_dir = write_secret(tmp_path, "demo_token", planted, mode=0o644)
        ctx = make_ctx(secrets_dir=secrets_dir)
        with pytest.raises(sdk.CommandError) as excinfo:
            ctx.secret("demo_token")
        assert excinfo.value.code == "secret_unreadable"
        assert "demo_token" in str(excinfo.value)
        assert planted not in str(excinfo.value)
        assert excinfo.value.__context__ is None
        assert excinfo.value.__cause__ is None

    def test_missing_secret_names_the_secret(self, tmp_path):
        secrets_dir = tmp_path / "secrets"
        secrets_dir.mkdir()
        ctx = make_ctx(secrets_dir=secrets_dir)
        with pytest.raises(sdk.CommandError) as excinfo:
            ctx.secret("demo_token")
        assert excinfo.value.code == "secret_unreadable"
        assert "demo_token" in str(excinfo.value)


class TestLog:
    def test_redacts_secret_values_from_formatted_records(self, tmp_path):
        secrets_dir = write_secret(tmp_path, "demo_token", "opaque-value-42")
        ctx = make_ctx(secrets_dir=secrets_dir)
        value = ctx.secret("demo_token")
        seen = []

        class Cap(logging.Handler):
            def emit(self, record):
                seen.append((record.getMessage(), record.args))

        handler = Cap()
        ctx.log.addHandler(handler)
        try:
            ctx.log.warning("login used %s for auth", value)
        finally:
            ctx.log.removeHandler(handler)
        assert seen
        assert seen[0][0] == "login used [redacted] for auth"
        assert seen[0][1] == ()

    def test_values_read_later_are_redacted_too(self, tmp_path):
        secrets_dir = write_secret(tmp_path, "first_token", "first-value-1")
        secrets_dir.joinpath("second_token").write_text("second-value-2")
        secrets_dir.joinpath("second_token").chmod(0o600)
        ctx = make_ctx(secrets_dir=secrets_dir)
        ctx.secret("first_token")
        seen = []

        class Cap(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        handler = Cap()
        ctx.log.addHandler(handler)
        try:
            ctx.log.warning("second secret is %s", ctx.secret("second_token"))
        finally:
            ctx.log.removeHandler(handler)
        assert seen == ["second secret is [redacted]"]

    def test_traceback_text_is_redacted(self, tmp_path):
        secrets_dir = write_secret(tmp_path, "demo_token", "opaque-value-42")
        ctx = make_ctx(secrets_dir=secrets_dir)
        value = ctx.secret("demo_token")
        seen = []

        class Cap(logging.Handler):
            def emit(self, record):
                seen.append(self.format(record))

        handler = Cap()
        ctx.log.addHandler(handler)
        try:
            try:
                raise RuntimeError(f"failed with {value}")
            except RuntimeError:
                ctx.log.exception("handler failed")
        finally:
            ctx.log.removeHandler(handler)
        assert len(seen) == 1
        assert "RuntimeError: failed with [redacted]" in seen[0]
        assert value not in seen[0]

    def test_contexts_do_not_share_filters(self, tmp_path):
        secrets_dir = write_secret(tmp_path, "demo_token", "opaque-value-42")
        first = make_ctx(secrets_dir=secrets_dir)
        first.secret("demo_token")
        second = make_ctx(secrets_dir=secrets_dir)
        assert first.log is not second.log
        assert len(first.log.filters) == 1
        assert len(second.log.filters) == 1
        assert logging.getLogger("housesitter.demo_sensor").filters == []

    def test_records_propagate_to_housesitter_logger(self):
        ctx = make_ctx()
        seen = []

        class Cap(logging.Handler):
            def emit(self, record):
                seen.append((record.name, record.getMessage()))

        parent = logging.getLogger("housesitter")
        handler = Cap()
        parent.addHandler(handler)
        try:
            ctx.log.warning("hello %s", "there")
        finally:
            parent.removeHandler(handler)
        assert seen == [("housesitter.demo_sensor", "hello there")]

    def test_adds_no_stdout_handlers(self):
        class OtherModule(DemoModule):
            id = "demo_other_logger"

        ctx = make_ctx(module=OtherModule())
        assert ctx.log.handlers == []
        assert ctx.log.name == "housesitter.demo_other_logger"


class TestStoreStub:
    def test_default_store_raises_not_implemented(self):
        store = make_ctx().store
        with pytest.raises(
            NotImplementedError, match="no store is attached"
        ):
            store.save("snapshot", {})
        with pytest.raises(
            NotImplementedError, match="no store is attached"
        ):
            store.list("snapshot")
        with pytest.raises(
            NotImplementedError, match="no store is attached"
        ):
            store.load("snapshot_20261104T151500Z_0001")

    def test_injected_store_is_returned(self):
        sentinel = object()
        assert make_ctx(store=sentinel).store is sentinel


class TestHttpBaseUrl:
    @pytest.mark.parametrize(
        "base_url",
        [
            "http://192.0.2.1/api/status-example",
            "http://user@192.0.2.1/",
            "http://user:@192.0.2.1/",
            "http://192.0.2.1 ",
            "http://192.0.2.1\t/",
            " http://192.0.2.1",
            "http://192.0.2.1\r\n/",
            "http://192.0.2.1/?section=clients",
            "http://192.0.2.1/#frag",
            "ftp://192.0.2.1/",
            "http:///no-host",
            "not a url at all",
        ],
    )
    def test_rejected(self, base_url):
        ctx = make_ctx()
        with pytest.raises(ValueError):
            ctx.http(base_url)

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://PLANTED-SECRET@192.0.2.1/",
            "https://192.0.2.1/PLANTED-SECRET",
            "https://192.0.2.1/?q=PLANTED-SECRET",
            "https://192.0.2.1:PLANTED-SECRET/",
            "ftp://PLANTED-SECRET/",
        ],
    )
    def test_error_never_echoes_the_url(self, base_url):
        ctx = make_ctx()
        with pytest.raises(ValueError) as caught:
            ctx.http(base_url)
        assert "PLANTED-SECRET" not in str(caught.value)
        assert caught.value.__context__ is None or "PLANTED-SECRET" not in str(
            caught.value.__context__
        )

    @pytest.mark.parametrize("timeout_s", [0, -5, float("nan"), float("inf"), True, "15"])
    def test_bad_timeout_rejected(self, timeout_s):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        with pytest.raises(ValueError):
            ctx.http("http://192.0.2.1", timeout_s=timeout_s)
        assert made == {}

    def test_timeout_passed_to_client(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("http://192.0.2.1", timeout_s=2.5)
        assert made["default_timeout_s"] == 2.5

    @pytest.mark.parametrize(
        "tls",
        [{"fingerprint_sha256": "ab" * 32}, {"insecure": True}, {"ca_file": "/x/ca.pem"}],
    )
    def test_tls_options_need_https(self, tls):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        with pytest.raises(ValueError):
            ctx.http("http://192.0.2.1", tls=tls)
        assert made == {}
        assert ctx.warnings == []

    @pytest.mark.parametrize(
        "tls",
        [
            {"insecure": 1},
            {"insecure": "yes"},
            {"insecure": 1.0},
            {"insecure": 0},
            {"ca_file": 7},
            {"fingerprint_sha256": b"ab" * 32},
            {"server_hostname": ["host.example"]},
        ],
    )
    def test_tls_values_need_exact_types(self, tls):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        with pytest.raises(ValueError):
            ctx.http("https://192.0.2.1", tls=tls)
        assert made == {}
        assert ctx.warnings == []

    def test_default_tls_section_works_with_http(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http(
            "http://192.0.2.1",
            tls={"ca_file": None, "server_hostname": None,
                 "fingerprint_sha256": None, "insecure": False},
        )
        assert made["tls"] == http_core.TlsConfig()

    @pytest.mark.parametrize(
        ("base_url", "scheme", "port"),
        [
            ("http://192.0.2.1", "http", 80),
            ("http://192.0.2.1/", "http", 80),
            ("http://192.0.2.1:8080", "http", 8080),
            ("https://192.0.2.1", "https", 443),
            ("https://192.0.2.1:8443/", "https", 8443),
        ],
    )
    def test_accepted(self, base_url, scheme, port):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http(base_url)
        assert (made["scheme"], made["host"], made["port"]) == (
            scheme,
            "192.0.2.1",
            port,
        )

    def test_factory_gets_module_allow_list(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("http://192.0.2.1")
        assert made["allow"] == DemoModule.http_allow

    def test_default_timeout_is_15(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("http://192.0.2.1")
        assert made["default_timeout_s"] == 15

    def test_tls_built_from_non_none_keys(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        pin = "ab" * 32
        ctx.http(
            "https://192.0.2.1",
            tls={
                "fingerprint_sha256": pin,
                "ca_file": None,
                "server_hostname": None,
                "insecure": None,
            },
        )
        assert isinstance(made["tls"], http_core.TlsConfig)
        assert made["tls"].fingerprint_sha256 == pin

    def test_unknown_tls_key(self):
        ctx = make_ctx()
        with pytest.raises(ValueError):
            ctx.http("https://192.0.2.1", tls={"cipher": "AES"})

    def test_insecure_warns_once(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("https://192.0.2.1", tls={"insecure": True})
        ctx.http("https://192.0.2.1", tls={"insecure": True})
        assert ctx.warnings == [
            "TLS verification disabled by config (demo_sensor.tls.insecure)"
        ]
        assert made["tls"].insecure is True

    def test_insecure_false_is_quiet(self):
        ctx = make_ctx(client_factory=make_factory({}))
        ctx.http("https://192.0.2.1", tls={"insecure": False})
        assert ctx.warnings == []


class TestSession:
    def test_json_body_and_content_type(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        response = ctx.http("http://192.0.2.1").post(
            "/api/status-example", json={"a": 1}
        )
        assert response.status == 200
        (call,) = made["client"].calls
        assert call["body"] == '{"a":1}'
        assert call["headers"] == {"Content-Type": "application/json"}
        assert call["deadline_s"] > 0

    def test_caller_content_type_kept(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("http://192.0.2.1").post(
            "/api/status-example",
            json={"a": 1},
            headers={"content-type": "text/custom"},
        )
        (call,) = made["client"].calls
        assert call["headers"] == {"content-type": "text/custom"}

    def test_get_passes_headers_through(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        ctx.http("http://192.0.2.1").get(
            "/api/status-example", headers={"X-Test": "1"}
        )
        (call,) = made["client"].calls
        assert call["method"] == "GET"
        assert call["headers"] == {"X-Test": "1"}
        assert call["body"] is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_statuses_become_upstream_auth(self, status):
        class AuthClient(FakeClient):
            def request(self, *args, **kwargs):
                self.calls.append(kwargs)
                return http_core.Response(status=status, headers={}, body=b"")

        ctx = make_ctx(client_factory=lambda *a, **k: AuthClient())
        with pytest.raises(http_core.UpstreamError) as excinfo:
            ctx.http("http://192.0.2.1").get("/api/status-example")
        assert excinfo.value.code == "upstream_auth"
        assert excinfo.value.status == status

    def test_other_statuses_pass_through(self):
        class TeapotClient(FakeClient):
            def request(self, *args, **kwargs):
                return http_core.Response(status=418, headers={}, body=b"")

        ctx = make_ctx(client_factory=lambda *a, **k: TeapotClient())
        response = ctx.http("http://192.0.2.1").get("/api/status-example")
        assert response.status == 418

    def test_expired_deadline_never_calls_client(self):
        made = {}
        ctx = make_ctx(
            client_factory=make_factory(made),
            deadline_monotonic=time.monotonic() - 1,
        )
        with pytest.raises(http_core.UpstreamError) as excinfo:
            ctx.http("http://192.0.2.1").get("/api/status-example")
        assert excinfo.value.code == "timeout"
        assert made["client"].calls == []

    def test_clear_auth_close_and_context_manager(self):
        made = {}
        ctx = make_ctx(client_factory=make_factory(made))
        session = ctx.http("http://192.0.2.1")
        with session:
            session.clear_auth()
        kinds = [
            "clear_auth" if "clear_auth" in call else "close" for call in made["client"].calls
        ]
        assert kinds == ["clear_auth", "close"]

    def test_real_client_end_to_end_get(self):
        seen = {}

        class SpyClient(http_core.Client):
            def request(self, *args, **kwargs):
                seen.update(kwargs)
                return super().request(*args, **kwargs)

        routes = {("GET", "/api/status-example"): Canned(status=200, body=b'{"a":1}')}
        with ScriptedServer(canned_handler(routes)) as server:
            ctx = make_ctx(
                client_factory=lambda *a, **k: SpyClient(*a, **k),
                deadline_monotonic=time.monotonic() + 10,
            )
            session = ctx.http(f"http://127.0.0.1:{server.port}")
            response = session.get("/api/status-example")
            assert response.status == 200
            assert response.body == b'{"a":1}'
            assert seen.get("deadline_s", 0) > 0
            assert len(server.requests) == 1
