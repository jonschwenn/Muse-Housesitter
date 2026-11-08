"""The read-only dashboard server (docs/DASHBOARD.md §5).

Serves the packaged ``web/`` files and ``GET /api/dashboard`` from saved
results only: it never contacts upstream systems, never triggers
collection, and never writes anything (no directory creation, no chmod,
no cache or pid files). GET and HEAD only; every other method gets 405.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import re
import socket
import stat as _stat
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from housesitter.core import dashboard as _dashboard
from housesitter.core.config import ConfigError, load_config
from housesitter.core.store import StoreError

# docs/DASHBOARD.md §5. Byte-identical to scripts/dev_dashboard.py's CSP:
# scripts and this server must send exactly the same headers.
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'"
)

_API_PATH = "/api/dashboard"
_INDEX_PATH = "/index.html"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

_UNAVAILABLE_BODY = b'{"error":"dashboard data unavailable"}'

_ERROR_BODIES = {
    400: b"bad request",
    404: b"not found",
    405: b"method not allowed",
    414: b"uri too long",
    421: b"unexpected host header",
    431: b"request too large",
    500: b"internal error",
    503: b"service unavailable",
    505: b"http version not supported",
}

# Request logs: drop ``user:pass@`` from absolute-form targets, and cap
# each line so a long request target cannot flood the Pi's SD card.
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s\"]*@")
_MAX_LOG_LINE = 500

# Hosts that always pass the Host check besides the bind address and the
# configured allow-list: loopback in every spelling browsers send.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})


class StartupError(Exception):
    """The server cannot start; carries its one stderr line."""


def web_dir() -> Path:
    """Where the packaged ``web/`` files live.

    Prefers ``housesitter.web`` inside the installed package
    (``importlib.resources``). ``web/`` ships without an ``__init__.py``
    so the source tree stays untouched, which makes it a namespace
    portion: on Python 3.9 ``resources.files`` cannot handle that (the
    portion has no ``origin``), so fall back to the package's own
    ``__path__``, which always names real filesystem directories. Last
    resort is the ``web/`` directory next to the source tree, for a
    checkout without an install.
    """
    try:
        ref = resources.files("housesitter.web")
    except Exception:
        ref = None
    if ref is not None:
        try:
            is_dir = ref.is_dir()
        except Exception:
            is_dir = False
        if is_dir and isinstance(ref, Path):
            return ref
    try:
        import housesitter.web as web_pkg

        for entry in web_pkg.__path__:
            candidate = Path(entry)
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    except Exception:
        pass
    # Editable installs always run from a source checkout (setuptools
    # cannot expose a namespace subpackage nested under the regular
    # ``housesitter`` package in editable mode), so the checkout's own
    # web/ directory is the honest fallback there.
    here = Path(__file__).resolve()
    return here.parents[3] / "web"


def load_static_files(directory: Path) -> dict:
    """Read every servable file directly in ``directory`` into memory.

    Only regular non-dot files with a servable extension, no subdirectories
    and no symlinks. Anything else is never served (404): dotfiles,
    DESIGN.md, and directory listings among them. Raises
    :class:`StartupError` when the install has no usable web files.
    """
    table: dict[str, tuple[bytes, str]] = {}
    try:
        names = sorted(entry.name for entry in directory.iterdir())
    except OSError:
        raise StartupError("web files missing from the install") from None
    for name in names:
        if name.startswith("."):
            continue
        path = directory / name
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode):
            continue
        if not _stat.S_ISREG(st.st_mode):
            continue
        content_type = _CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        table["/" + name] = (data, content_type)
    if _INDEX_PATH not in table:
        raise StartupError("web files missing from the install")
    return table


def host_name(header: str) -> str:
    """The host part of a Host header, without the port.

    Bracketed IPv6 keeps its brackets; a single colon separates a port;
    anything else (bare IPv6, no port) passes through whole. Lowercase:
    Host comparison is case-insensitive.
    """
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def host_allowed(header_values: list, bind: str, allowed: Any) -> bool:
    """True when the request's Host header passes the DNS-rebinding check.

    Exactly one Host header must be present, and its hostname (port
    stripped, case-insensitive) must be the bind address, loopback, or a
    name in the configured allow-list. Missing or duplicate headers fail.
    """
    if len(header_values) != 1:
        return False
    name = _bare(host_name(header_values[0]))
    if not name:
        return False
    # Compare IPv6 without brackets on both sides: browsers always send
    # ``[2001:db8::5]:8080``, while config may list ``2001:db8::5``.
    return name == _bare(bind.lower()) or name in {_bare(str(h).lower()) for h in allowed}


def _bare(name: str) -> str:
    """``[2001:db8::5]`` becomes ``2001:db8::5``; anything else is unchanged."""
    if name.startswith("[") and name.endswith("]"):
        return name[1:-1]
    return name


class DashboardHandler(BaseHTTPRequestHandler):
    """GET/HEAD handler for static files and /api/dashboard."""

    server_version = "housesitter"
    sys_version = ""
    timeout = 10
    # Requests that fail before their version is parsed (a garbled request
    # line, an unsupported version) would otherwise get an HTTP/0.9-style
    # reply with no status line or headers. Answering them as HTTP/1.0
    # keeps the security headers on every response.
    default_request_version = "HTTP/1.0"

    # Set per server by create_server.
    static_files: dict = {}
    bind_host: str = "127.0.0.1"
    allowed_hosts: frozenset = frozenset()
    build_view: Any = None

    def log_message(self, fmt: str, *args: Any) -> None:
        """Log to stderr with control characters and non-printables
        escaped. Only the rendered request line is logged, never headers.
        Python 3.9 does not escape these itself."""
        try:
            text = fmt % args  # noqa: UP031 - stdlib supplies % placeholders
        except Exception:
            text = fmt
        text = _USERINFO_RE.sub(r"\1", text)
        safe = "".join(
            char
            if char.isprintable()
            else f"\\x{ord(char):02x}"
            if ord(char) < 256
            else f"\\u{ord(char):04x}"
            for char in text
        )
        if len(safe) > _MAX_LOG_LINE:
            safe = safe[: _MAX_LOG_LINE - 3] + "..."
        try:
            sys.stderr.write(safe + "\n")
            sys.stderr.flush()
        except Exception:
            pass

    def send_error(self, code: int, message: Any = None,
                   explain: Any = None) -> None:
        """Fixed short plain-text bodies, never the stdlib HTML page.

        The stdlib error page echoes the request path; these bodies name
        nothing from the request. Every error still carries the security
        headers, exactly like a success.
        """
        body = _ERROR_BODIES.get(code, b"error")
        self._send(code, body, "text/plain; charset=utf-8")

    def _headers(self, code: int, content_type: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self._headers(code, content_type, len(body))
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _check_host(self) -> bool:
        """True when the Host header passes; else answers 421."""
        try:
            values = self.headers.get_all("Host", [])
        except Exception:
            values = []
        if host_allowed(values or [], self.bind_host, self.allowed_hosts):
            return True
        self.send_error(421)
        return False

    @staticmethod
    def _static_path(raw_path: str) -> str | None:
        """Map a request path to a static table key, or None for 404.

        Percent-escapes, ``..`` segments, backslashes, and NUL bytes are
        refused before the table lookup; the query string is ignored for
        routing. Only exact table keys serve.
        """
        if (
            "%" in raw_path
            or "\\" in raw_path
            or "\x00" in raw_path
        ):
            return None
        if any(
            part in (".", "..") for part in raw_path.split("/")
        ):
            return None
        if ".." in raw_path:
            return None
        return raw_path

    def _serve_static(self, path: str) -> None:
        key = self._static_path(path)
        if key == "/":
            key = _INDEX_PATH
        entry = self.static_files.get(key) if key is not None else None
        if entry is None:
            self.send_error(404)
            return
        data, content_type = entry
        if self.command == "HEAD":
            self._headers(200, content_type, len(data))
            return
        self._send(200, data, content_type)

    def _serve_api(self) -> None:
        try:
            view = self.build_view()
        except (
            _dashboard.DashboardUnavailable, StoreError, ConfigError,
        ):
            self._send(
                503, _UNAVAILABLE_BODY,
                "application/json; charset=utf-8",
            )
            return
        except Exception:
            self.send_error(500)
            return
        try:
            payload = json.dumps(
                view, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            self.send_error(500)
            return
        self._send(200, payload, "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 (stdlib naming)
        if not self._check_host():
            return
        path = urlsplit(self.path).path
        if path == _API_PATH:
            self._serve_api()
            return
        self._serve_static(path)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if not self._check_host():
            return
        path = urlsplit(self.path).path
        if path == _API_PATH:
            self._serve_api()
            return
        self._serve_static(path)

    def _read_only(self) -> None:
        self.send_error(405, "read-only")

    def __getattr__(self, name: str) -> Any:
        # BaseHTTPRequestHandler answers 501 for methods it has no do_X
        # for. Every method other than GET and HEAD gets 405 instead.
        if name.startswith("do_"):
            return self._read_only
        raise AttributeError(name)


class DashboardServer(ThreadingHTTPServer):
    """Threaded HTTP server for the dashboard. Threads are daemonic so a
    Ctrl-C shutdown never hangs on an open connection."""

    daemon_threads = True


def create_server(
    bind: str,
    port: int,
    *,
    registry: Any,
    config_loader: Any,
    env: Any = None,
    static_files: dict | None = None,
    web_directory: Path | None = None,
) -> DashboardServer:
    """Build (but do not start) the dashboard server.

    The view is built per request through ``config_loader`` so an unsafe
    data dir or bad config answers 503 instead of serving stale data;
    the bind address, port, and Host allow-list stay fixed for the
    server's life. Raises :class:`StartupError` when the web files are
    missing, and OSError when the address cannot be bound.
    """
    if static_files is None:
        static_files = load_static_files(
            web_directory if web_directory is not None else web_dir()
        )
    try:
        version = ipaddress.ip_address(bind).version
    except ValueError:
        raise StartupError(
            f"invalid dashboard bind address {bind!r}"
        ) from None
    # Read the config once at startup for the Host allow-list; per-request
    # loads still govern the view itself. A config failure here is a
    # startup error, not a 503: ConfigError keeps its key and problem so
    # the CLI can reuse the config-check error line format.
    try:
        startup_config = config_loader()
    except ConfigError:
        raise
    except StoreError as exc:
        raise StartupError(str(exc)) from None

    def build_view() -> dict:
        return _dashboard.build_view(
            registry=registry, config=config_loader(), env=env
        )

    handler = type(
        "BoundDashboardHandler",
        (DashboardHandler,),
        {
            "static_files": static_files,
            "bind_host": bind,
            "allowed_hosts": frozenset(
                _LOCAL_HOSTS
                | {bind.lower()}
                | {
                    str(item).lower()
                    for item in startup_config.dashboard.allowed_hosts
                }
            ),
            "build_view": staticmethod(build_view),
        },
    )
    if version == 6:
        class DashboardServer6(DashboardServer):
            address_family = socket.AF_INET6

        server_cls = DashboardServer6
    else:
        server_cls = DashboardServer
    return server_cls((bind, port), handler)


def _bind_error_line(bind: str, port: int, exc: OSError) -> str:
    """One clear stderr line for a bind failure, naming bind:port."""
    target = f"{bind}:{port}"
    number = exc.errno
    if number == errno.EADDRINUSE:
        return (
            f"housesitter: serve: cannot bind {target}: address already in "
            "use; stop the other server or change "
            "housesitter.dashboard.port"
        )
    if number in (errno.EADDRNOTAVAIL,):
        return (
            f"housesitter: serve: cannot bind {target}: address is not "
            "available on this machine"
        )
    if number in (errno.EACCES, errno.EPERM):
        return (
            f"housesitter: serve: cannot bind {target}: permission denied "
            "(ports below 1024 need privileges)"
        )
    return f"housesitter: serve: cannot bind {target}: {exc.strerror or 'bind failed'}"


def display_host(bind: str) -> str:
    """The bind address as it goes into the startup URL (brackets for v6)."""
    return f"[{bind}]" if ":" in bind else bind


def serve_command(registry: Any, env: Any, out: Any, err: Any) -> int:
    """Run ``housesitter serve``: bind and serve until Ctrl-C.

    Returns the process exit code: 0 on Ctrl-C, 2 on any startup error
    with exactly one stderr line.
    """
    try:
        config = load_config(registry, env=env)
    except ConfigError as exc:
        err.write(f"housesitter: housesitter: config_invalid: {exc.key}: {exc.problem}\n")
        return 2
    bind = config.dashboard.bind
    port = config.dashboard.port

    def config_loader() -> Any:
        return load_config(registry, env=env)

    try:
        server = create_server(
            bind, port, registry=registry, config_loader=config_loader,
            env=env,
        )
    except ConfigError as exc:
        err.write(
            f"housesitter: housesitter: config_invalid: "
            f"{exc.key}: {exc.problem}\n"
        )
        return 2
    except StartupError as exc:
        err.write(f"housesitter: serve: {exc}\n")
        return 2
    except OSError as exc:
        err.write(_bind_error_line(bind, port, exc) + "\n")
        return 2
    out.write(
        f"serving the dashboard on http://{display_host(bind)}:{port}/ (read-only)\n"
    )
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:  # pragma: no cover - config already validated it
        address = None
    if address is not None and not address.is_loopback:
        out.write(
            "reminder: this server is reachable on your network; anyone who "
            "can reach this port sees device names, MACs, and IPs\n"
        )
    try:
        out.flush()
    except Exception:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    return 0
