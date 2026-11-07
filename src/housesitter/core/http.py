"""The only network client (ARCHITECTURE §3, ``core/http.py``).

Every module gets its sessions through here, bound to one scheme, host,
and port plus an allow-list of ``(method, path-regex)`` pairs. Before any
socket is opened the request target is normalised and ``re.fullmatch``ed;
anything else raises :class:`NotAllowed`. Redirects are never followed:
a 3xx response becomes an ``upstream_unexpected`` error (the numeric
status is kept on the error's ``status`` attribute so callers such as
flavour detection can tell a redirect apart without reading headers).

TLS modes: system verification by default, a CA file (with an optional
``server_hostname`` override for IP hosts), a pinned SHA-256 leaf
fingerprint checked right after the handshake and **before any request
bytes are sent**, or ``insecure`` only when passed explicitly.

Cookies and CSRF tokens are kept in memory only. Response bodies are
capped at 8 MiB, and a truncated body (fewer bytes than the framing
promises) is an ``upstream_unexpected`` error, never a short success.
``Content-Length`` must be decimal and non-negative, with every repeated
value agreeing. Request timeouts are bounded by the caller's deadline:
each stage waits at most the time left, and a watchdog shuts the live
socket down when the deadline passes, so even a byte-at-a-time drip
fails as ``timeout`` within the deadline plus a small margin.
Error text never contains secrets, request bodies, response bodies, or
cookie values: messages carry only the method, the path, and numeric
statuses.

Seams (later PRs): ``core/config.py`` builds the ``TlsConfig`` from the
module's ``tls`` section; ``Context`` passes ``ctx.deadline`` (seconds
left) as ``deadline_s`` and maps 401/403 responses to ``upstream_auth``.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import re
import socket
import ssl
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# §5-scale cap: response (and request) bodies are cut off above this.
MAX_BODY_BYTES = 8 * 1024 * 1024

_CHUNK = 64 * 1024

# 3xx codes that ask the caller to go elsewhere. Any other 3xx (300,
# 304, 306) is reported as an unexpected status, never as a redirect.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 305, 307, 308})

_CSRF_REQUEST_HEADER = "X-CSRF-Token"
_CSRF_RESPONSE_HEADERS = ("x-csrf-token", "x-updated-csrf-token")

# Characters that must never appear in a request target or header field.
_BAD_TARGET_RE = re.compile(r"[\x00-\x20\x7f]")
# Header fields may hold spaces, but never CR, LF, or NUL (injection).
_BAD_HEADER_RE = re.compile(r"[\x00\r\n]")


class NotAllowed(Exception):
    """The method/target pair is not in the session's allow-list."""


class UpstreamError(Exception):
    """A request failed after passing the allow-list.

    ``code`` is one of the stable envelope codes (``upstream_unreachable``,
    ``upstream_tls``, ``upstream_unexpected``, ``timeout``). ``status``
    carries the numeric HTTP status when one was received (including 3xx,
    which is never followed), else ``None``. The message is author-written
    and never echoes secrets, bodies, cookies, or upstream text.
    """

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Allow:
    """One allow-list entry: an HTTP method plus a path pattern.

    The pattern is matched with ``re.fullmatch`` against the normalised
    request target. Query strings (``?...``) are refused unless
    ``allow_query`` is set, in which case the ``?query`` suffix is split
    off and only the path part is matched. Percent-escapes and ``..``
    segments are always refused: no module needs them, and encoded
    traversal must not depend on how carefully a pattern is written.
    """

    method: str
    pattern: str
    allow_query: bool = False
    _compiled: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "method", self.method.upper())
        try:
            compiled = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"invalid allow-list pattern: {self.pattern!r}") from exc
        object.__setattr__(self, "_compiled", compiled)

    def matches(self, method: str, path: str) -> bool:
        return self.method == method.upper() and self._compiled.fullmatch(path) is not None


@dataclass(frozen=True)
class TlsConfig:
    """How to verify (or not) the upstream TLS certificate.

    Default is system verification. Alternatives: ``ca_file`` (with an
    optional ``server_hostname`` override used for SNI and verification
    when ``host`` is an IP the certificate does not cover), or
    ``fingerprint_sha256`` (a pinned leaf SHA-256, ``AA:BB:…`` or bare
    hex, checked before any request bytes are sent). ``insecure``
    disables verification and must be passed explicitly; it combines
    with nothing.
    """

    ca_file: str | None = None
    server_hostname: str | None = None
    fingerprint_sha256: str | None = None
    insecure: bool = False
    _pin: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.insecure and (
            self.ca_file is not None or self.fingerprint_sha256 is not None
        ):
            raise ValueError("insecure TLS combines with no other option")
        if self.ca_file is not None and not self.ca_file:
            raise ValueError("ca_file must not be empty")
        if self.server_hostname is not None:
            if not self.server_hostname or _BAD_TARGET_RE.search(self.server_hostname):
                raise ValueError("invalid server_hostname")
            if self.fingerprint_sha256 is not None or self.insecure:
                raise ValueError("server_hostname needs verified TLS (system or ca_file)")
        pin: str | None = None
        if self.fingerprint_sha256 is not None:
            pin = re.sub(r"[:\s]", "", self.fingerprint_sha256).lower()
            if re.fullmatch(r"[0-9a-f]{64}", pin) is None:
                raise ValueError("fingerprint_sha256 must be 32 hex bytes")
        object.__setattr__(self, "_pin", pin)


@dataclass(frozen=True)
class Response:
    """A non-redirect upstream response: numeric status, headers, raw body."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        """Parse the body as JSON, mapping any failure to upstream_unexpected."""
        import json

        # The raise sits outside the except block so the decoder's
        # exception (which quotes the upstream body) never survives on
        # __context__; see Client.request for the same pattern.
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            error = UpstreamError("upstream_unexpected", "response is not JSON")
        raise error


def _normalise_target(target: str) -> tuple[str, str | None]:
    """Split and validate a request target, refusing escapes and traversal.

    Returns ``(path, query_or_None)``. Raises :class:`NotAllowed` for
    anything non-canonical: missing leading ``/``, control characters or
    spaces, fragments, ``%``-escapes, empty segments (``//``), ``.`` or
    ``..`` segments.
    """
    if not isinstance(target, str) or not target.startswith("/"):
        # The raw target is never interpolated: it may hold a secret smuggled
        # in as a non-absolute target (e.g. "password=...").
        raise NotAllowed("not allowed: request target is not an absolute path")
    if _BAD_TARGET_RE.search(target):
        raise NotAllowed("not allowed: request target holds control characters")
    if "#" in target:
        raise NotAllowed("not allowed: request target must not hold a fragment")
    if "%" in target:
        raise NotAllowed("not allowed: request target must not hold escapes")
    path, sep, query = target.partition("?")
    segments = path.split("/")[1:]
    if any(seg in ("", ".", "..") for seg in segments):
        raise NotAllowed("not allowed: request target is not canonical")
    return path, (query if sep else None)


class _DeadlineWatch:
    """A hard total bound for one request.

    Per-call socket timeouts cannot bound a drip: ``http.client`` loops
    ``recv`` inside a single ``getresponse``/``read`` call, and every
    ``recv`` gets a fresh timeout. The watch fires once, ``delay_s``
    after the request started: it records the hit and shuts the live
    socket down, so whatever call is blocked wakes immediately and the
    stage maps it to ``timeout``.

    The abort targets a ``dup`` of the socket file descriptor taken
    while the request owns it. ``shutdown`` is connection-scoped, so it
    wakes readers through any file object sharing the connection
    (``connection.sock``, or ``response.fp`` after a ``Connection:
    close`` response detaches the socket). The ``dup`` is held open
    until :meth:`close`, and ``_done`` is set under the same lock that
    guards the shutdown, so a late fire can never touch a descriptor
    that has been closed and reused by an unrelated socket. :meth:`close`
    cancels and joins the timer, so no request leaves a thread behind.
    """

    def __init__(self, delay_s: float) -> None:
        self._lock = threading.Lock()
        self._done = False
        self._hit = False
        self._fds: list[int] = []
        self._timer = threading.Timer(delay_s, self._fire)
        self._timer.daemon = True

    def start(self) -> None:
        self._timer.start()

    @property
    def expired(self) -> bool:
        with self._lock:
            return self._hit

    def track(self, sock: Any) -> None:
        """Remember the live socket behind one connection object."""
        try:
            dup = os.dup(sock.fileno())
        except OSError:
            return
        with self._lock:
            if self._done:
                try:
                    os.close(dup)
                except OSError:
                    pass
            else:
                self._fds.append(dup)

    def _fire(self) -> None:
        with self._lock:
            if self._done:
                return
            self._hit = True
            for fd in self._fds:
                # Shut the connection down through a throwaway wrapper:
                # the objects the request used may be flagged closed
                # (detached after getresponse, or mid-TLS), and methods
                # on them can refuse to act, while this fd is provably
                # still ours — the dup below keeps it from being reused.
                try:
                    wrapper = socket.socket(fileno=fd)
                except OSError:
                    continue
                try:
                    wrapper.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                finally:
                    wrapper.detach()

    def close(self) -> None:
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            self._done = True
            fds, self._fds = self._fds, []
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


class _WatchedMixin:
    """Reports each live socket to the request's deadline watch.

    ``Client`` sets ``_watch`` per request (``None`` when the call
    passes no deadline, in which case reporting is a no-op).
    """

    _watch: _DeadlineWatch | None = None

    def _note(self) -> None:
        watch = self._watch
        sock = self.sock
        if watch is not None and sock is not None:
            watch.track(sock)


class _WatchedHTTPConnection(_WatchedMixin, http.client.HTTPConnection):
    def connect(self) -> None:
        super().connect()
        self._note()


class _WatchedHTTPSConnection(_WatchedMixin, http.client.HTTPSConnection):
    def connect(self) -> None:
        # Same steps as HTTPSConnection.connect, but the raw socket is
        # reported before the TLS handshake so the watch can abort a
        # handshake that only ever drips.
        http.client.HTTPConnection.connect(self)
        self._note()
        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=server_hostname
        )
        self._note()


class _PinnedHTTPSConnection(_WatchedMixin, http.client.HTTPSConnection):
    """HTTPSConnection with an explicit verification hostname (SNI + check)."""

    def __init__(self, *args: Any, server_hostname: str, **kwargs: Any) -> None:
        self._override_hostname = server_hostname
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        super(http.client.HTTPSConnection, self).connect()
        self._note()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._override_hostname
        )
        self._note()


class Client:
    """An allow-listed session bound to one scheme, host, and port.

    ``allow`` is a sequence of :class:`Allow` entries (or
    ``(method, pattern)`` / ``(method, pattern, allow_query)`` tuples).
    ``default_timeout_s`` bounds each request when the call passes no
    deadline; ``deadline_s`` on a call is a total bound for the whole
    request — connect, TLS handshake, send, headers, and body share the
    seconds left before the caller's deadline.
    """

    def __init__(
        self,
        scheme: str,
        host: str,
        port: int,
        allow: Sequence[Allow | tuple],
        tls: TlsConfig | None = None,
        default_timeout_s: float = 15,
    ) -> None:
        if scheme not in ("http", "https"):
            raise ValueError(f"scheme must be http or https, not {scheme!r}")
        if not isinstance(host, str) or not host or _BAD_TARGET_RE.search(host):
            raise ValueError(f"invalid host: {host!r}")
        if any(char in host for char in "/?#@"):
            raise ValueError(f"invalid host: {host!r}")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"invalid port: {port!r}")
        tls = tls or TlsConfig()
        if scheme == "http" and (
            tls.fingerprint_sha256 is not None
            or tls.ca_file is not None
            or tls.server_hostname is not None
            or tls.insecure
        ):
            raise ValueError("TLS options need the https scheme")
        entries: list[Allow] = []
        for entry in allow:
            if isinstance(entry, Allow):
                entries.append(entry)
            elif isinstance(entry, tuple) and len(entry) in (2, 3):
                entries.append(Allow(*entry))
            else:
                raise ValueError(f"invalid allow-list entry: {entry!r}")
        if not entries:
            raise ValueError("allow-list must not be empty")
        if default_timeout_s <= 0:
            raise ValueError("default_timeout_s must be positive")
        self.scheme = scheme
        self.host = host
        self.port = port
        self.allow = tuple(entries)
        self.tls = tls
        self.default_timeout_s = default_timeout_s
        self._cookies: dict[str, str] = {}
        self._csrf_token: str | None = None
        self._closed = False

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Client({self.scheme}://{self.host}:{self.port}, "
            f"{len(self.allow)} allow entries)"
        )

    def close(self) -> None:
        self._closed = True
        self.clear_auth()

    def clear_auth(self) -> None:
        """Drop in-memory cookies and the CSRF token (e.g. after logout)."""
        self._cookies = {}
        self._csrf_token = None

    def _check_allowed(self, method: str, target: str) -> str:
        path, query = _normalise_target(target)
        upper = method.upper()
        for entry in self.allow:
            if entry.method != upper:
                continue
            if query is not None and not entry.allow_query:
                continue
            if entry.matches(upper, path):
                return target
        # The message carries the method and bare path only: query values
        # stay out of error text in case a module ever puts data there.
        raise NotAllowed(f"not allowed: {upper} {path}")

    def _context(self) -> ssl.SSLContext:
        tls = self.tls
        if tls.insecure or tls.fingerprint_sha256 is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        if tls.ca_file is not None:
            try:
                return ssl.create_default_context(cafile=tls.ca_file)
            except OSError:
                raise UpstreamError("upstream_tls", "CA file cannot be read") from None
        try:
            return ssl.create_default_context()
        except OSError:
            raise UpstreamError("upstream_tls", "TLS system setup failed") from None

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.scheme == "http":
            return _WatchedHTTPConnection(self.host, self.port, timeout=timeout)
        context = self._context()
        if self.tls.server_hostname is not None:
            return _PinnedHTTPSConnection(
                self.host,
                self.port,
                timeout=timeout,
                context=context,
                server_hostname=self.tls.server_hostname,
            )
        return _WatchedHTTPSConnection(
            self.host, self.port, timeout=timeout, context=context
        )

    def request(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | None = None,
        timeout_s: float | None = None,
        deadline_s: float | None = None,
    ) -> Response:
        """Send one allow-listed request and return the response.

        ``deadline_s`` is seconds left before the caller's deadline; the
        whole request — connect, TLS handshake, send, headers, and body —
        shares that budget as a hard total bound. Two lines of defence
        enforce it: every stage waits at most ``min(limit, remaining)``
        where ``limit`` is ``timeout_s`` (or the default) narrowed once
        by ``deadline_s``, and a :class:`_DeadlineWatch` shuts the live
        socket down when the deadline passes, waking whatever
        ``http.client`` call is blocked inside a drip. Raises
        :class:`NotAllowed` before any socket opens, and
        :class:`UpstreamError` after.

        ``UpstreamError`` never carries ``__cause__`` or ``__context__``:
        each failure is built inside its ``except`` block but raised after
        it, so chained exceptions (which may quote upstream bytes) cannot
        keep upstream objects alive or leak into tracebacks.
        """
        if self._closed:
            raise UpstreamError("upstream_unexpected", "client is closed")
        request_target = self._check_allowed(method, target)
        upper = method.upper()

        payload: bytes | None = None
        if body is not None:
            payload = body.encode("utf-8") if isinstance(body, str) else bytes(body)
            if len(payload) > MAX_BODY_BYTES:
                raise UpstreamError("upstream_unexpected", "request body exceeds 8 MiB limit")

        outgoing: dict[str, str] = {}
        if headers:
            for name, value in headers.items():
                if not isinstance(name, str) or not isinstance(value, str):
                    raise ValueError("header names and values must be strings")
                if _BAD_HEADER_RE.search(name) or _BAD_HEADER_RE.search(value):
                    raise ValueError("header holds control characters")
                if name.lower() in ("host", "content-length"):
                    raise ValueError("header must not be set by the caller")
                outgoing[name] = value
        lowered = {name.lower() for name in outgoing}
        if self._cookies and "cookie" not in lowered:
            outgoing["Cookie"] = "; ".join(f"{key}={val}" for key, val in self._cookies.items())
        if upper != "GET" and self._csrf_token is not None and "x-csrf-token" not in lowered:
            outgoing[_CSRF_REQUEST_HEADER] = self._csrf_token

        limit = self.default_timeout_s if timeout_s is None else timeout_s
        if deadline_s is not None:
            limit = min(limit, deadline_s)
        if limit <= 0:
            raise UpstreamError("timeout", "deadline already passed")
        start = time.monotonic()

        def budget() -> float:
            if deadline_s is None:
                return limit
            remaining = deadline_s - (time.monotonic() - start)
            if remaining <= 0:
                raise UpstreamError("timeout", "deadline exceeded")
            return min(limit, remaining)

        watch: _DeadlineWatch | None = None
        if deadline_s is not None:
            delay = deadline_s - (time.monotonic() - start)
            if delay <= 0:
                raise UpstreamError("timeout", "deadline exceeded")
            watch = _DeadlineWatch(delay)

        connection = self._connection(budget())
        connection._watch = watch
        if watch is not None:
            watch.start()

        def classify(code: str, message: str) -> UpstreamError:
            """Map a stage failure, preferring the watch when it fired.

            When the deadline passed mid-stage, whatever exception the
            abort caused is a timeout, not the underlying error.
            """
            if watch is not None and watch.expired:
                return UpstreamError("timeout", "deadline exceeded")
            return UpstreamError(code, message)

        def apply_budget() -> None:
            """Enforce the deadline and shrink the socket timeout to fit it.

            The expiry check always runs: after ``getresponse`` hands a
            ``Connection: close`` body to the response, ``connection.sock``
            is detached (``None``), but the wall-clock bound still applies.
            """
            if deadline_s is None:
                return
            timeout = budget()
            sock = connection.sock
            if sock is not None:
                try:
                    sock.settimeout(timeout)
                except OSError:
                    sock_broken = True
                else:
                    sock_broken = False
                if sock_broken:
                    # Raised outside the except block so the OSError (which
                    # may quote socket details) never lands on __context__.
                    raise classify("upstream_unexpected", "socket failed")

        error: UpstreamError | None = None
        try:
            # Connect up front (TCP, plus the TLS handshake for https) so
            # every later stage can be bounded by the shrinking budget.
            # Errors here are classified by what failed: refused means
            # nothing is listening; anything else after TCP accepted on an
            # https connection means the TLS handshake itself failed (for a
            # pinned session this includes a plain-HTTP impostor).
            try:
                connection.connect()
            except socket.timeout:
                error = classify("timeout", "connection timed out")
            except ssl.SSLError:
                error = classify("upstream_tls", "TLS handshake failed")
            except ConnectionRefusedError:
                error = classify("upstream_unreachable", "cannot reach upstream")
            except OSError:
                if self.scheme == "https" and self.tls.fingerprint_sha256 is not None:
                    error = classify("upstream_tls", "TLS handshake failed")
                else:
                    error = classify("upstream_unreachable", "cannot reach upstream")
            if error is not None:
                raise error
            if self.tls.fingerprint_sha256 is not None:
                sock = connection.sock
                der = sock.getpeercert(binary_form=True) if sock is not None else None
                digest = hashlib.sha256(der).hexdigest() if der else ""
                if digest != self.tls._pin:
                    raise UpstreamError("upstream_tls", "certificate fingerprint mismatch")
            try:
                apply_budget()
                connection.request(upper, request_target, body=payload, headers=outgoing)
            except socket.timeout:
                error = classify("timeout", "request timed out")
            except ssl.SSLError:
                error = classify("upstream_tls", "TLS handshake failed")
            except OSError:
                error = classify("upstream_unreachable", "cannot reach upstream")
            if error is not None:
                raise error
            try:
                apply_budget()
                response = connection.getresponse()
            except socket.timeout:
                error = classify("timeout", "request timed out")
            except ssl.SSLError:
                error = classify("upstream_tls", "TLS handshake failed")
            except (
                http.client.BadStatusLine,
                http.client.RemoteDisconnected,
            ):
                error = classify("upstream_unexpected", "upstream closed the connection")
            except http.client.HTTPException:
                error = classify("upstream_unexpected", "upstream response is malformed")
            except OSError:
                error = classify("upstream_unreachable", "cannot reach upstream")
            if error is not None:
                raise error
            if 300 <= response.status <= 399:
                if response.status in _REDIRECT_STATUSES:
                    raise UpstreamError(
                        "upstream_unexpected",
                        f"unexpected redirect (status {response.status})",
                        status=response.status,
                    )
                raise UpstreamError(
                    "upstream_unexpected",
                    f"unexpected status (status {response.status})",
                    status=response.status,
                )
            raw_headers = response.getheaders()
            merged: dict[str, str] = {}
            for name, value in raw_headers:
                key = name.lower()
                merged[key] = value if key not in merged else merged[key] + ", " + value
            expected = self._body_length(upper, response.status, merged)
            chunks: list[bytes] = []
            received = 0
            while True:
                # Bound each read by the remaining budget: the body may
                # already sit in the client's buffer (keep-alive) or arrive
                # in drips (stall). The watch is the hard bound underneath:
                # one read call loops ``recv`` internally, and every
                # ``recv`` gets a fresh socket timeout, so the per-read
                # budget alone can never stop a drip. An empty read or a
                # closed response ends the body; anything else maps below.
                apply_budget()
                try:
                    piece = response.read(min(_CHUNK, MAX_BODY_BYTES + 1 - received))
                except socket.timeout:
                    error = classify("timeout", "request timed out")
                except (
                    http.client.IncompleteRead,
                    http.client.RemoteDisconnected,
                ):
                    error = classify("upstream_unexpected", "upstream closed the connection")
                except http.client.HTTPException:
                    error = classify("upstream_unexpected", "upstream response is malformed")
                except OSError:
                    error = classify("upstream_unexpected", "failed reading response")
                if error is not None:
                    raise error
                if not piece:
                    break
                received += len(piece)
                if received > MAX_BODY_BYTES:
                    raise UpstreamError("upstream_unexpected", "response body exceeds 8 MiB limit")
                chunks.append(piece)
                if response.isclosed():
                    break
            if expected is not None and received < expected:
                # The framing promised more bytes than arrived: the body
                # was truncated (FIN, RST, or a clean TLS close mid-body).
                # A fired watch means the deadline caused the short read.
                raise classify("upstream_unexpected", "upstream closed the connection")
            if watch is not None and watch.expired:
                raise UpstreamError("timeout", "deadline exceeded")
            self._remember_auth(raw_headers)
            return Response(status=response.status, headers=merged, body=b"".join(chunks))
        finally:
            if watch is not None:
                watch.close()
            connection.close()

    @staticmethod
    def _body_length(method: str, status: int, headers: Mapping[str, str]) -> int | None:
        """Return the framed body length, or ``None`` when unknown.

        ``Content-Length`` must be decimal and non-negative, and every
        repeated value must agree; anything else is a malformed upstream
        response. Chunked framing, HEAD, and the statuses that forbid a
        body (1xx, 204, 304) carry no length check: ``http.client``
        defines their bodies as empty.
        """
        if method == "HEAD" or status == 204 or status == 304 or 100 <= status < 200:
            return None
        transfer = headers.get("transfer-encoding", "")
        if "chunked" in {token.strip().lower() for token in transfer.split(",")}:
            return None
        raw = headers.get("content-length")
        if raw is None:
            return None
        values = []
        for part in raw.split(","):
            text = part.strip()
            if re.fullmatch(r"[0-9]+", text) is None:
                # Raised here, not in an except block, so no upstream
                # bytes can land on __context__; the message is fixed text.
                raise UpstreamError(
                    "upstream_unexpected", "upstream sent an invalid Content-Length"
                )
            values.append(int(text, 10))
        if any(value != values[0] for value in values[1:]):
            raise UpstreamError(
                "upstream_unexpected", "upstream sent an invalid Content-Length"
            )
        return values[0]

    def _remember_auth(self, raw_headers: list[tuple[str, str]]) -> None:
        for name, value in raw_headers:
            lowered = name.lower()
            if lowered == "set-cookie":
                pair = value.split(";", 1)[0]
                key, sep, val = pair.partition("=")
                key = key.strip()
                if sep and key and _BAD_HEADER_RE.search(key + val) is None:
                    self._cookies[key] = val.strip()
            elif lowered in _CSRF_RESPONSE_HEADERS and value.strip():
                if _BAD_HEADER_RE.search(value.strip()) is None:
                    self._csrf_token = value.strip()
