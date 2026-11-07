"""The concrete handler context (core-internal).

Modules see only the ``sdk.Context`` protocol. This class binds a module
instance to its validated config, secrets directory, deadline, clock, HTTP
client factory, and store, and hands handlers allow-listed HTTP sessions.
"""

from __future__ import annotations

import json as _json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from housesitter import sdk
from housesitter.core import http as _http
from housesitter.core import secrets as _secrets

# Whitespace and control characters never belong in a base URL; refuse
# them up front instead of failing later with a confusing DNS error.
_BAD_URL_RE = re.compile(r"[\x00-\x20\x7f]")

_TLS_KEYS = frozenset(
    {"ca_file", "server_hostname", "fingerprint_sha256", "insecure"}
)


def _freeze(obj: Any) -> Any:
    """Deep read-only copy: dicts to proxy, lists/tuples to tuple."""
    if isinstance(obj, Mapping):
        return MappingProxyType({key: _freeze(value) for key, value in obj.items()})
    if isinstance(obj, (list, tuple)):
        return tuple(_freeze(value) for value in obj)
    return obj


class _StubStore:
    """Placeholder until the store lands: every method refuses."""

    def save(self, kind: str, obj: dict, label: str | None = None) -> str:
        raise NotImplementedError("store arrives in 12b")

    def list(self, kind: str, offset: int = 0, limit: int = 20) -> sdk.StorePage:
        raise NotImplementedError("store arrives in 12b")

    def load(self, id: str) -> dict:
        raise NotImplementedError("store arrives in 12b")


class _RedactingFilter(logging.Filter):
    """Replace every known secret value in the formatted record text."""

    def __init__(self, values: Callable[[list], list]) -> None:
        super().__init__()
        self._values = values

    def _redact(self, text: str) -> str:
        for value in self._values():
            if value and value in text:
                text = text.replace(value, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:  # pragma: no cover - defensive: never break logging
            return True
        redacted = self._redact(text)
        if redacted != text:
            record.msg = redacted
            record.args = ()
        # Tracebacks can quote secret values too: format them once here,
        # redact, and drop the live exception so handlers use the text.
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = self._redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redact(record.stack_info)
        return True


class _Session:
    """One allow-listed session bound to a module context."""

    def __init__(self, ctx: ModuleContext, client: _http.Client) -> None:
        self._ctx = ctx
        self._client = client

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> _http.Response:
        if self._ctx.deadline <= 0:
            raise _http.UpstreamError(
                "timeout", "deadline passed before the request was sent"
            )
        outgoing = dict(headers) if headers else {}
        body = None
        if json is not None:
            body = _json.dumps(json, separators=(",", ":"), ensure_ascii=False)
            if not any(name.lower() == "content-type" for name in outgoing):
                outgoing["Content-Type"] = "application/json"
        response = self._client.request(
            method, path, headers=outgoing, body=body,
            deadline_s=self._ctx.deadline,
        )
        if response.status in (401, 403):
            raise _http.UpstreamError(
                "upstream_auth",
                f"upstream refused the request (status {response.status})",
                status=response.status,
            )
        return response

    def get(self, path: str, **kw: Any) -> _http.Response:
        return self.request("GET", path, **kw)

    def post(
        self, path: str, *, json: Any = None, **kw: Any
    ) -> _http.Response:
        return self.request("POST", path, json=json, **kw)

    def clear_auth(self) -> None:
        self._client.clear_auth()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class ModuleContext:
    """The concrete ``sdk.Context``. Core-internal; modules see the protocol."""

    def __init__(
        self,
        module: sdk.Module,
        config: Mapping[str, Any],
        *,
        secrets_dir: Any,
        deadline_monotonic: float,
        now: Callable[[], datetime] | None = None,
        client_factory: Callable[..., _http.Client] | None = None,
        store: Any = None,
    ) -> None:
        self._module = module
        self._config = _freeze(dict(config))
        self._secrets_dir = secrets_dir
        self._deadline_monotonic = float(deadline_monotonic)
        self._now = now
        self._client_factory = client_factory or _http.Client
        self._store = store if store is not None else _StubStore()
        self.warnings: list[str] = []
        self._secret_values: list[str] = []
        # A fresh logger per context, outside the logging manager, so the
        # redacting filter (and the secrets it holds) dies with the context
        # instead of piling up on a shared ``housesitter.<id>`` logger.
        # Records still propagate to the ``housesitter`` logger's handlers.
        logger = logging.Logger(f"housesitter.{module.id}")
        logger.parent = logging.getLogger("housesitter")
        logger.addFilter(_RedactingFilter(lambda: list(self._secret_values)))
        self._log = logger

    @property
    def module_id(self) -> str:
        return self._module.id

    @property
    def config(self) -> Mapping[str, Any]:
        return self._config

    @property
    def store(self) -> Any:
        return self._store

    @property
    def log(self) -> logging.Logger:
        return self._log

    @property
    def deadline(self) -> float:
        return max(0.0, self._deadline_monotonic - time.monotonic())

    def now(self) -> datetime:
        value = self._now() if self._now is not None else datetime.now(timezone.utc)
        if not isinstance(value, datetime):
            raise ValueError("now() must return a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now() must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def secret(self, name: str) -> str:
        try:
            value = _secrets.read_secret(name, self._secrets_dir)
        except _secrets.SecretError as exc:
            error = sdk.CommandError(
                "secret_unreadable", f"cannot read secret {name!r}: {exc.problem}"
            )
        else:
            self._secret_values.append(value)
            return value
        raise error

    def http(
        self,
        base_url: str,
        *,
        tls: Mapping[str, Any] | None = None,
        timeout_s: float | None = None,
    ) -> _Session:
        # Every error below is raised outside its ``except`` block and never
        # quotes base_url: a URL may carry userinfo (a password), and the
        # parser's own messages echo the text into ``__context__``.
        parts = None
        if isinstance(base_url, str):
            try:
                parts = urlsplit(base_url)
            except ValueError:
                parts = None
        if parts is None or _BAD_URL_RE.search(base_url):
            raise ValueError("invalid base_url")
        if parts.scheme not in ("http", "https"):
            raise ValueError("base_url scheme must be http or https")
        if not parts.hostname:
            raise ValueError("base_url needs a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url must not hold userinfo")
        if parts.query or parts.fragment:
            raise ValueError(
                "base_url must not hold a query or fragment"
            )
        if parts.path not in ("", "/"):
            raise ValueError("base_url must not hold a path")
        try:
            port = parts.port
            bad_port = False
        except ValueError:
            bad_port = True
        if bad_port:
            raise ValueError("base_url holds an invalid port")
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if timeout_s is None:
            timeout_s = 15
        elif (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive number of seconds")
        if tls is None:
            tls_config = _http.TlsConfig()
        else:
            if not isinstance(tls, Mapping):
                raise ValueError("tls must be a mapping")
            unknown = set(tls) - _TLS_KEYS
            if unknown:
                raise ValueError(f"unknown tls keys: {sorted(unknown)}")
            # Exact types: a truthy non-bool ``insecure`` (1, "yes") would
            # otherwise disable verification without the warning below.
            insecure = tls.get("insecure")
            if insecure is not None and not isinstance(insecure, bool):
                raise ValueError("tls insecure must be true or false")
            for key in ("ca_file", "server_hostname", "fingerprint_sha256"):
                value = tls.get(key)
                if value is not None and not isinstance(value, str):
                    raise ValueError(f"tls {key} must be a string")
            options = {
                key: tls[key] for key in _TLS_KEYS if tls.get(key) is not None
            }
            if options.get("insecure") is False:
                del options["insecure"]
            if options and parts.scheme != "https":
                raise ValueError("tls options need an https base_url")
            tls_config = _http.TlsConfig(**options)
            if options.get("insecure") is True:
                warning = (
                    "TLS verification disabled by config "
                    f"({self._module.id}.tls.insecure)"
                )
                if warning not in self.warnings:
                    self.warnings.append(warning)
        client = self._client_factory(
            parts.scheme,
            parts.hostname,
            port,
            allow=self._module.http_allow,
            tls=tls_config,
            default_timeout_s=timeout_s,
        )
        return _Session(self, client)
