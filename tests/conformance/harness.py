"""Fixture harness for the module conformance suite.

A fake upstream is served by the existing ``ScriptedServer`` from
``tests/core/helpers.py`` on 127.0.0.1. ``client_factory`` records the
``(scheme, host, port)`` the module asked for, then returns a REAL
``housesitter.core.http.Client`` pointed at the fixture server. The TLS
config is dropped: the fixture speaks plain HTTP. This way the real
allow-list check, framing, auth cookies, the 401/403 mapping, and
redirect refusal all run.

Each run gets a fresh temp HOME, a config file enabling the module with
the manifest's config, a secrets dir, and a captured log stream.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.helpers import Canned, ScriptedServer, canned_handler
from housesitter.core import envelope, runner
from housesitter.core import http as http_core
from housesitter.core.registry import Registry
from housesitter.core.store import FileStore

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures"

_MANIFEST_KEYS = frozenset({"config", "secrets", "spec", "cases"})
_CASE_KEYS = frozenset({"name", "fixture", "command", "params", "routes", "expect"})
_ROUTE_KEYS = frozenset({"method", "path", "status", "body", "body_file", "headers"})


def load_manifest(module_id: str, fixture_dir: Path) -> dict:
    """Read and strictly validate ``conformance.json`` for one module."""
    path = Path(fixture_dir) / "conformance.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"missing conformance manifest: {path}") from None
    except ValueError:
        raise ValueError(
            f"conformance manifest is not valid JSON: {path}"
        ) from None
    if not isinstance(manifest, dict):
        raise ValueError(f"conformance manifest must hold an object: {path}")
    unknown = set(manifest) - _MANIFEST_KEYS
    if unknown:
        raise ValueError(f"manifest holds unknown keys: {sorted(unknown)}")
    missing = _MANIFEST_KEYS - set(manifest)
    if missing:
        raise ValueError(f"manifest is missing keys: {sorted(missing)}")
    if not isinstance(manifest["config"], dict):
        raise ValueError("manifest 'config' must be an object")
    secrets = manifest["secrets"]
    if not isinstance(secrets, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in secrets.items()
    ):
        raise ValueError("manifest 'secrets' must map names to string values")
    if not isinstance(manifest["spec"], str) or not manifest["spec"]:
        raise ValueError("manifest 'spec' must be a non-empty path")
    cases = manifest["cases"]
    if not isinstance(cases, list) or not cases:
        raise ValueError("manifest 'cases' must be a non-empty list")
    seen = set()
    for case in cases:
        _validate_case(case, seen, Path(fixture_dir))
    return manifest


def _validate_case(case: Any, seen: set, fixture_dir: Path) -> None:
    if not isinstance(case, dict):
        raise ValueError(f"manifest case must hold an object: {case!r}")
    unknown = set(case) - _CASE_KEYS
    if unknown:
        raise ValueError(f"case holds unknown keys: {sorted(unknown)}")
    for key in ("name", "fixture", "command", "routes", "expect"):
        if key not in case:
            raise ValueError(f"case is missing {key!r}: {case!r}")
    name = case["name"]
    if not isinstance(name, str) or not name:
        raise ValueError(f"case name must be a non-empty string: {case!r}")
    if name in seen:
        raise ValueError(f"duplicate case name: {name!r}")
    seen.add(name)
    for key in ("fixture", "command"):
        if not isinstance(case[key], str) or not case[key]:
            raise ValueError(f"case {key!r} must be a non-empty string: {name!r}")
    params = case.get("params", {})
    if not isinstance(params, dict):
        raise ValueError(f"case params must be an object: {name!r}")
    routes = case["routes"]
    if not isinstance(routes, list) or not routes:
        raise ValueError(f"case routes must be a non-empty list: {name!r}")
    for route in routes:
        _validate_route(route, name, fixture_dir)
    expect = case["expect"]
    if expect in ("ok", "truncated"):
        return
    if (
        isinstance(expect, dict)
        and set(expect) == {"error"}
        and expect["error"] in envelope.ERROR_CODES
    ):
        return
    raise ValueError(f"case expect is invalid: {name!r}: {expect!r}")


def _validate_route(route: Any, case_name: str, fixture_dir: Path) -> None:
    if not isinstance(route, dict):
        raise ValueError(f"route must hold an object: case {case_name!r}")
    unknown = set(route) - _ROUTE_KEYS
    if unknown:
        raise ValueError(f"route holds unknown keys: {sorted(unknown)}")
    for key in ("method", "path", "status"):
        if key not in route:
            raise ValueError(f"route is missing {key!r}: case {case_name!r}")
    if not isinstance(route["method"], str) or not route["method"]:
        raise ValueError(f"route method must be a string: case {case_name!r}")
    if not isinstance(route["path"], str) or not route["path"].startswith("/"):
        raise ValueError(f"route path must be absolute: case {case_name!r}")
    status = route["status"]
    if (
        not isinstance(status, int)
        or isinstance(status, bool)
        or not 100 <= status <= 599
    ):
        raise ValueError(f"route status must be 100..599: case {case_name!r}")
    if "body" in route and "body_file" in route:
        raise ValueError(f"route holds both body and body_file: case {case_name!r}")
    if "body" in route and not isinstance(route["body"], str):
        raise ValueError(f"route body must be a string: case {case_name!r}")
    if "body_file" in route:
        if not isinstance(route["body_file"], str):
            raise ValueError(f"route body_file must be a string: case {case_name!r}")
        if not (fixture_dir / route["body_file"]).is_file():
            raise ValueError(
                f"route body_file is missing: case {case_name!r}: "
                f"{route['body_file']!r}"
            )
    if "headers" in route:
        headers = route["headers"]
        if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in headers.items()
        ):
            raise ValueError(f"route headers must be strings: case {case_name!r}")


def _harvest_bodies(route: dict, fixture_dir: Path, harvest) -> None:
    """Feed one route's body and header values to ``harvest``.

    An inline ``body`` parses as JSON when it parses (so secret leaves
    are recognised individually); otherwise the literal text is used.
    String header values are harvested too.
    """
    if "body" in route:
        try:
            harvest(json.loads(route["body"]))
        except ValueError:
            harvest(route["body"])
    if "body_file" in route:
        harvest(
            json.loads(
                (Path(fixture_dir) / route["body_file"]).read_text(encoding="utf-8")
            )
        )
    for value in route.get("headers", {}).values():
        harvest(value)


def planted_strings(manifest: dict, fixture_dir: Path) -> set:
    """Every string containing the planted-secret marker for one module."""
    found = set()

    def harvest(value: Any) -> None:
        if isinstance(value, str):
            if "PLANTED-SECRET" in value:
                found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                harvest(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                harvest(item)

    for value in manifest["secrets"].values():
        harvest(value)
    for case in manifest["cases"]:
        for route in case["routes"]:
            _harvest_bodies(route, fixture_dir, harvest)
    return found


def upstream_string_leaves(manifest: dict, fixture_dir: Path) -> set:
    """Every JSON string leaf in the module's fixture bodies."""
    found = set()

    def harvest(value: Any) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                harvest(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                harvest(item)

    for case in manifest["cases"]:
        for route in case["routes"]:
            _harvest_bodies(route, fixture_dir, harvest)
    return found


def build_routes(route_dicts: list, fixture_dir: Path) -> dict:
    """Canned ``{(method, target): Canned}`` responses for route entries."""
    table = {}
    for route in route_dicts:
        if "body_file" in route:
            body = (Path(fixture_dir) / route["body_file"]).read_bytes()
        elif "body" in route:
            body = route["body"].encode("utf-8")
        else:
            body = b""
        table[(route["method"].upper(), route["path"])] = Canned(
            status=route["status"],
            headers=dict(route.get("headers", {})),
            body=body,
        )
    return table


def recording_subclass(module_cls: type) -> tuple:
    """Wrap each handler so the RAW ``Result`` is recorded before the runner
    projects it. The subclass keeps the same id, commands, and skill file
    resolution (``__module__`` is preserved for the skill lookup).

    The wrap resolves the class attribute so static, class, and instance
    handlers all keep their binding: a staticmethod stays a staticmethod,
    a classmethod stays a classmethod, and a plain method stays a method.
    """

    def _wrap_instance(handler, handler_name: str, records: list):
        def recording(self, ctx, params):
            result = handler(self, ctx, params)
            # Keyed by handler: commands may share one (round-3 nit 6).
            records.append((handler_name, result))
            return result

        return recording

    def _wrap_static(handler, handler_name: str, records: list):
        @staticmethod
        def recording(*args, **kwargs):
            result = handler(*args, **kwargs)
            records.append((handler_name, result))
            return result

        return recording

    def _wrap_class(handler, handler_name: str, records: list):
        @classmethod
        def recording(cls, *args, **kwargs):
            result = handler(*args, **kwargs)
            records.append((handler_name, result))
            return result

        return recording

    records: list = []
    namespace = {}
    for command in module_cls.commands:
        if command.handler not in namespace:
            static = inspect.getattr_static(module_cls, command.handler)
            original = getattr(module_cls, command.handler)
            if isinstance(static, staticmethod):
                namespace[command.handler] = _wrap_static(
                    original, command.handler, records
                )
            elif isinstance(static, classmethod):
                namespace[command.handler] = _wrap_class(
                    original, command.handler, records
                )
            else:
                namespace[command.handler] = _wrap_instance(
                    original, command.handler, records
                )
    subclass = type(module_cls.__name__ + "Recording", (module_cls,), namespace)
    subclass.__module__ = module_cls.__module__
    return subclass, records


def _substitute_ports(value: Any, port: int) -> Any:
    if isinstance(value, str):
        return value.replace("{port}", str(port))
    if isinstance(value, dict):
        return {key: _substitute_ports(item, port) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_substitute_ports(item, port) for item in value]
    return value


@dataclass
class SingleRun:
    """One ``run_command`` invocation against the fixture server."""

    module_cls: type
    module_id: str
    full_name: str
    command_name: str
    case: dict | None
    outcome: Any
    stdout_text: str
    stderr_line: str | None
    raw_results: list
    requests: list
    connections: int
    factory_calls: list
    log_stream_text: str
    log_records: list
    home: Path
    data_dir: Path
    config_used: dict
    secrets_used: dict
    server_port: int
    saved_obj: dict | None = None
    params: dict = field(default_factory=dict)
    stdout_captured: str = ""
    stderr_captured: str = ""
    fd_stdout_captured: bytes = b""
    fd_stderr_captured: bytes = b""
    saved_history: list = field(default_factory=list)


class _CaptureHandler(logging.Handler):
    """Collect fully formatted log records (all levels) during one run.

    The formatter renders the message plus any ``exc_info`` traceback
    and ``stack_info`` text, so a secret smuggled into an exception or
    a stack frame is visible to the secrets scan.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter())
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            try:
                self.records.append(record.getMessage())
            except Exception:
                self.records.append("<unformattable log message>")


class CaseSession:
    """One temp HOME plus a recording registry for a module's cases."""

    def __init__(
        self,
        module_cls: type,
        manifest: dict,
        tmp_path: Path,
        fixture_dir: Path,
        retention: int = 500,
        identifiers: str = "full",
    ) -> None:
        self.module_cls = module_cls
        self.module_id = module_cls.id
        self.manifest = manifest
        self.fixture_dir = Path(fixture_dir)
        self.retention = retention
        self.identifiers = identifiers
        self.home = Path(tmp_path) / "home"
        (self.home / ".config" / "housesitter").mkdir(parents=True, exist_ok=True)
        self.env = {"HOME": str(self.home)}
        self.recording_cls, self.records = recording_subclass(module_cls)
        self.registry = Registry.from_classes([self.recording_cls])
        self.config_path = self.home / ".config" / "housesitter" / "config.json"
        self.secrets_dir = self.home / ".config" / "housesitter" / "secrets"

    def run(
        self, full_name: str, params: dict, route_dicts: list, case: dict | None = None
    ) -> SingleRun:
        """Serve ``route_dicts`` on a fixture server and invoke the command."""
        server = ScriptedServer(
            canned_handler(build_routes(route_dicts, self.fixture_dir))
        )
        try:
            return self._run_with_server(server, full_name, params, case)
        finally:
            server.close()

    def _run_with_server(
        self, server: ScriptedServer, full_name: str, params: dict, case: dict | None
    ) -> SingleRun:
        config = _substitute_ports(
            dict(self.manifest["config"]), server.port
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "housesitter": {
                        "identifiers": self.identifiers,
                        "retention": self.retention,
                    },
                    "modules": {
                        self.module_id: {"enabled": True, **config}
                    },
                }
            ),
            encoding="utf-8",
        )
        os.makedirs(self.secrets_dir, exist_ok=True)
        os.chmod(self.secrets_dir, 0o700)
        for name, value in self.manifest["secrets"].items():
            path = self.secrets_dir / name
            path.write_text(value, encoding="utf-8")
            os.chmod(path, 0o600)

        factory_calls: list[dict] = []

        def factory(scheme, host, port, **kwargs):
            factory_calls.append(
                {"scheme": scheme, "host": host, "port": port}
            )
            return http_core.Client(
                "http",
                "127.0.0.1",
                server.port,
                allow=kwargs.get("allow"),
                default_timeout_s=kwargs.get("default_timeout_s"),
            )

        from housesitter.core import store as _store_module

        saved_payloads: list[str] = []
        real_save = _store_module.FileStore.save

        def recording_save(self, kind, obj=None, label=None, **kwargs):
            try:
                saved_payloads.append(json.dumps(obj, sort_keys=True, default=str))
            except (TypeError, ValueError):
                saved_payloads.append(repr(obj))
            return real_save(self, kind, obj, label, **kwargs)

        stream = io.StringIO()
        capture = _CaptureHandler()
        logger = logging.getLogger("housesitter")
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(capture)
        marked = len(self.records)
        # The handler runs in a worker thread, so only a module-level swap
        # of sys.stdout/sys.stderr catches stray writes from any thread.
        # The runner's own log stream is separate; anything landing here is
        # the module's, and stray stdout would corrupt the SDK's JSON.
        old_stdout, old_stderr = sys.stdout, sys.stderr
        out_capture, err_capture = io.StringIO(), io.StringIO()
        # An env mutation in one case must never reach the next.
        old_environ = dict(os.environ)
        _store_module.FileStore.save = recording_save
        sys.stdout, sys.stderr = out_capture, err_capture
        # File-descriptor capture: the sys swap above misses writes that
        # bypass the swapped objects, so fds 1 and 2 are also redirected
        # for the run. This catches os.write(1/2, ...), writes through
        # sys.__stdout__/__stderr__, and writes through stdout/stderr
        # object references a module saved earlier: every spelling lands
        # on the same file descriptors. Threads that outlive the run and
        # output at import time are outside this capture (documented
        # limits, not proven here).
        fd_saved = []
        fd_files = {}
        for number in (1, 2):
            try:
                saved = os.dup(number)
            except OSError:
                continue
            tmp = tempfile.TemporaryFile(mode="w+b")
            try:
                os.dup2(tmp.fileno(), number)
            except OSError:
                os.close(saved)
                tmp.close()
                continue
            fd_saved.append((number, saved))
            fd_files[number] = tmp
        try:
            outcome = runner.run_command(
                full_name,
                params,
                registry=self.registry,
                env=self.env,
                client_factory=factory,
                log_stream=stream,
            )
        finally:
            # Only bytes that actually reached fds 1/2 during the run are
            # attributed to it. Buffered objects (such as sys.__stdout__)
            # are deliberately NOT flushed here: flushing could pull in
            # output buffered before the run and blame it on the module.
            sys.stdout, sys.stderr = old_stdout, old_stderr
            for number, saved in fd_saved:
                try:
                    os.dup2(saved, number)
                except OSError:
                    pass
                finally:
                    os.close(saved)
            _store_module.FileStore.save = real_save
            os.environ.clear()
            os.environ.update(old_environ)
            logger.removeHandler(capture)
            logger.setLevel(old_level)
        fd_out = b""
        fd_err = b""
        for number, tmp in fd_files.items():
            try:
                tmp.seek(0)
                content = tmp.read()
            except OSError:
                content = b""
            finally:
                tmp.close()
            if number == 1:
                fd_out = content
            else:
                fd_err = content
        stdout_text = envelope.serialize(outcome.envelope)
        stderr_line = None
        if outcome.exit_code != 0:
            stderr_line = runner.format_error_line(outcome)
        saved_obj = None
        if outcome.envelope.get("saved"):
            saved_path = (
                self.data_dir()
                / (str(outcome.envelope["saved"]) + ".json")
            )
            if saved_path.is_file():
                saved_obj = json.loads(saved_path.read_text(encoding="utf-8"))
        return SingleRun(
            module_cls=self.module_cls,
            module_id=self.module_id,
            full_name=full_name,
            command_name=full_name.split(".")[-1],
            case=case,
            outcome=outcome,
            stdout_text=stdout_text,
            stderr_line=stderr_line,
            raw_results=[result for _, result in self.records[marked:]],
            requests=list(server.requests),
            connections=server.connections,
            factory_calls=factory_calls,
            log_stream_text=stream.getvalue(),
            log_records=list(capture.records),
            home=self.home,
            data_dir=self.data_dir(),
            config_used=config,
            secrets_used=dict(self.manifest["secrets"]),
            server_port=server.port,
            saved_obj=saved_obj,
            params=dict(params),
            stdout_captured=out_capture.getvalue(),
            stderr_captured=err_capture.getvalue(),
            fd_stdout_captured=fd_out,
            fd_stderr_captured=fd_err,
            saved_history=saved_payloads,
        )

    def data_dir(self) -> Path:
        """The module's data directory under the temp HOME."""
        return (
            self.home / ".local" / "share" / "housesitter" / self.module_id
        )

    def run_case(self, case_name: str) -> SingleRun:
        """Run one manifest case by name."""
        for case in self.manifest["cases"]:
            if case["name"] == case_name:
                full_name = f"housesitter.{self.module_id}.{case['command']}"
                return self.run(
                    full_name, dict(case.get("params", {})), case["routes"], case
                )
        raise ValueError(f"unknown case: {case_name!r}")

    def follow_paging(self, first: SingleRun, limit: int = 50) -> list:
        """Replay ``more`` until it is null, returning the follow-up runs."""
        follows = []
        current = first
        while current.outcome.envelope.get("more") and len(follows) < limit:
            more = current.outcome.envelope["more"]
            current = self.run(more["command"], dict(more["params"]), [], None)
            follows.append(current)
        return follows

    def context_for(self, server_port: int, timeout_s: float = 60.0):
        """A live module Context whose HTTP sessions reach one server."""
        from housesitter.core.context import ModuleContext

        config = _substitute_ports(dict(self.manifest["config"]), server_port)
        return ModuleContext(
            self.module_cls(),
            config,
            secrets_dir=self.secrets_dir,
            deadline_monotonic=time.monotonic() + timeout_s,
            store=FileStore(self.module_id, env=self.env),
        )


def modules_under_test() -> list:
    """Built-in modules plus the test-only demo module.

    Returns ``[(module_cls, fixture_dir)]`` sorted by module id. Third-party
    entry points are intentionally excluded.
    """
    from demo_module import ConformanceDemo

    from housesitter.core.registry import load_registry

    entries = []
    for entry in load_registry(include_entry_points=False).modules:
        entries.append((entry.cls, FIXTURES_ROOT / entry.id))
    entries.append((ConformanceDemo, FIXTURES_ROOT / "conformance_demo"))
    return sorted(entries, key=lambda item: item[0].id)


def case_string_leaves(case: dict, fixture_dir: Path) -> set:
    """Every JSON string leaf in one case's fixture bodies."""
    found = set()

    def harvest(value: Any) -> None:
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                harvest(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                harvest(item)

    for route in case["routes"]:
        _harvest_bodies(route, fixture_dir, harvest)
    return found


def page_runs(entry: dict, case_name: str) -> list:
    """One case's first run plus every follow-up page, in order."""
    return [entry["runs"][case_name]] + entry["follows"].get(case_name, [])


def config_implied_binding(config: dict) -> tuple:
    """The ``(scheme, host, port)`` the module's config asks the client for."""
    parts = urlsplit(config["base_url"])
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (parts.scheme, parts.hostname, port)
