"""In-process HTTP(S) test servers: local sockets on 127.0.0.1 only.

``ScriptedServer`` speaks just enough HTTP to test the core client:
one request per connection, canned responses, slow bodies, and TLS via
a throwaway self-signed cert generated at test time (never committed).
It records every accepted connection, every byte received after the TLS
handshake, and every parsed request, so tests can prove negatives such
as "a disallowed path never opened a socket".
"""

from __future__ import annotations

import hashlib
import shutil
import socket
import ssl
import subprocess
import threading
from dataclasses import dataclass, field


@dataclass
class Request:
    method: str
    target: str
    headers: dict
    body: bytes


@dataclass
class Canned:
    status: int = 200
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    sleep_s: float = 0.0
    drip_s: float = 0.0
    raw: bytes | None = None


def generate_cert(directory):
    """Create a throwaway self-signed cert; skip when openssl is missing.

    Returns ``(cert_path, key_path, sha256_hex_of_leaf_der)``.
    """
    if shutil.which("openssl") is None:
        import pytest

        pytest.skip("openssl is not on PATH: cannot mint a throwaway test cert")
    cert_path = str(directory / "cert.pem")
    key_path = str(directory / "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key_path, "-out", cert_path, "-days", "1",
            "-nodes", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    with open(cert_path, encoding="utf-8") as handle:
        pem = handle.read()
    der = ssl.PEM_cert_to_DER_cert(pem)
    return cert_path, key_path, hashlib.sha256(der).hexdigest()


class ScriptedServer:
    """A tiny scripted HTTP server over a plain or TLS socket."""

    join_timeout_s = 5.0  # per thread, in close()

    def __init__(self, handler, tls_cert=None, tls_key=None):
        self._handler = handler
        self.connections = 0
        self.peer_bytes: list[bytes] = []
        self.requests: list[Request] = []
        self._lock = threading.Lock()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        # close() wakes accept() with shutdown(); the short timeout is only
        # a fallback for platforms where shutdown() does not wake it.
        self._sock.settimeout(0.05)
        self.port = self._sock.getsockname()[1]
        if tls_cert is not None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls_cert, tls_key)
            self._tls = context
        else:
            self._tls = None
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        """Stop accepting, then wait for every connection to finish.

        When this returns, ``connections``, ``peer_bytes`` and ``requests``
        are final: no connection thread is still recording. A connection
        still sending is waited for (each handshake or recv times out after
        2 s). Canned sleeps end early once the stop flag is set, and a
        canned drip sends the rest of its body without the delays. If a
        thread is still alive after its own 5 s join (``join_timeout_s``, per
        thread, so N stuck threads take about 5 x N s), this raises rather than
        let a test read partial records.
        """
        self._stop.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes accept() on Linux
        except OSError:
            pass
        self._thread.join(timeout=self.join_timeout_s)
        stuck = self._thread.is_alive()
        for worker in list(self._workers):
            worker.join(timeout=self.join_timeout_s)
            stuck = stuck or worker.is_alive()
        self._sock.close()
        if stuck:
            raise AssertionError("ScriptedServer.close(): a server thread is still running")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self._serve_one, args=(raw,), daemon=True)
            self._workers.append(thread)
            thread.start()

    def _serve_one(self, raw):
        with self._lock:
            self.connections += 1
        sock = raw
        try:
            raw.settimeout(2.0)
            if self._tls is not None:
                try:
                    sock = self._tls.wrap_socket(raw, server_side=True)
                except (ssl.SSLError, OSError):
                    raw.close()
                    return
            sock.settimeout(2.0)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                    if len(data) > 32 * 1024 * 1024:
                        break
            except (socket.timeout, ssl.SSLError, OSError):
                pass
            with self._lock:
                self.peer_bytes.append(data)
            if not data or b"\r\n" not in data:
                sock.close()
                return
            head, _, rest = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            try:
                method, target, _ = lines[0].decode("latin-1").split(" ", 2)
            except ValueError:
                sock.close()
                return
            headers: dict[str, str] = {}
            for line in lines[1:]:
                name, sep, value = line.decode("latin-1").partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            length = int(headers.get("content-length", "0") or "0")
            body = rest[:length]
            while len(body) < length:
                try:
                    chunk = sock.recv(min(65536, length - len(body)))
                except (socket.timeout, ssl.SSLError, OSError):
                    break
                if not chunk:
                    break
                body += chunk
                with self._lock:
                    self.peer_bytes[-1] = self.peer_bytes[-1] + chunk
            request = Request(method=method, target=target, headers=headers, body=body)
            with self._lock:
                self.requests.append(request)
            canned = self._handler(request)
            if canned.sleep_s:
                self._stop.wait(canned.sleep_s)
            text = f"HTTP/1.1 {canned.status} Reason\r\n".encode("latin-1")
            outgoing = dict(canned.headers)
            outgoing.setdefault("Content-Length", str(len(canned.body)))
            outgoing.setdefault("Connection", "close")
            for name, value in outgoing.items():
                text += f"{name}: {value}\r\n".encode("latin-1")
            text += b"\r\n"
            if canned.raw is not None:
                text = canned.raw
            try:
                sock.sendall(text)
            except OSError:
                pass
            if canned.raw is not None:
                sock.close()
                return
            if canned.drip_s:
                for index in range(0, len(canned.body), 1024):
                    try:
                        sock.sendall(canned.body[index:index + 1024])
                    except OSError:
                        break
                    self._stop.wait(canned.drip_s)
                sock.close()
                return
            text = canned.body
            try:
                sock.sendall(text)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        except OSError:
            try:
                raw.close()
            except OSError:
                pass


def canned_handler(routes, default=None):
    """Build a handler from a ``{(method, target): Canned}`` map."""
    fallback = default or Canned(status=404, body=b"not found")

    def handle(request):
        return routes.get((request.method, request.target), fallback)

    return handle
