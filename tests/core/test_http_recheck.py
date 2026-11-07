"""Regression tests for the core HTTP client re-check (findings 1-6).

Every test here pins one finding: a drip that must fail within
``deadline + 0.5`` s, a truncated or malformed body that must fail as
``upstream_unexpected``, and the smaller contract fixes (304 wording,
``http`` + ``insecure``). Drip servers use raw sockets so each byte can
be paced; nothing here touches the network beyond 127.0.0.1.
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time

import pytest

from housesitter.core.http import (
    Allow,
    Client,
    TlsConfig,
    UpstreamError,
)

from .helpers import Canned, ScriptedServer, canned_handler, generate_cert

ALLOW = [Allow("GET", r"/stat/sta"), Allow("GET", r"/s")]

DEADLINE_S = 1.0
# A total bound must fire within the deadline plus a small margin for
# wake-up and error mapping, never a multiple of the deadline.
ELAPSED_LIMIT_S = DEADLINE_S + 0.5

ALLOW_S = [Allow("GET", r"/s")]


def _serve_raw(reply_head: bytes, drips: list[bytes], drip_s: float,
               holder: list[int]) -> None:
    """Serve one connection: head, then each drip spaced by ``drip_s``."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    holder.append(listener.getsockname()[1])

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                conn.sendall(reply_head)
                for drip in drips:
                    time.sleep(drip_s)
                    try:
                        conn.sendall(drip)
                    except OSError:
                        break
            except OSError:
                pass
        listener.close()

    threading.Thread(target=serve, daemon=True).start()


def _drip_request(head: bytes, drips: list[bytes], drip_s: float = 0.4):
    holder: list[int] = []
    _serve_raw(head, drips, drip_s, holder)
    time.sleep(0.1)
    client = Client("http", "127.0.0.1", holder[0], ALLOW_S,
                    default_timeout_s=10)
    started = time.monotonic()
    try:
        with client:
            client.request("GET", "/s", deadline_s=DEADLINE_S)
    except UpstreamError as exc:
        return exc, time.monotonic() - started
    raise AssertionError("drip-feed succeeded, deadline was not a total bound")


def test_body_drip_times_out_within_deadline():
    """Finding 1: 1 byte every 0.4 s with deadline 1.0 must time out fast."""
    head = (b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n"
            b"Connection: close\r\n\r\n")
    exc, elapsed = _drip_request(head, [b"x"] * 20)
    assert exc.code == "timeout"
    assert elapsed < ELAPSED_LIMIT_S


def test_chunked_body_drip_times_out_within_deadline():
    """Finding 1 (chunked): one 1-byte chunk every 0.4 s must time out."""
    head = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n")
    drips = [b"1\r\nx\r\n"] * 10 + [b"0\r\n\r\n"]
    exc, elapsed = _drip_request(head, drips)
    assert exc.code == "timeout"
    assert elapsed < ELAPSED_LIMIT_S


def test_chunk_header_drip_times_out_within_deadline():
    """A chunk-size line dripped 1 byte every 0.4 s must time out."""
    head = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n")
    drips = [bytes([byte]) for byte in b"5\r\nhello\r\n0\r\n\r\n"]
    exc, elapsed = _drip_request(head, drips)
    assert exc.code == "timeout"
    assert elapsed < ELAPSED_LIMIT_S


def test_header_drip_times_out_within_deadline():
    """Finding 3: headers dripped 2 bytes every 0.4 s must time out."""
    full = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
    drips = [full[index:index + 2] for index in range(0, len(full), 2)]
    exc, elapsed = _drip_request(b"", drips)
    assert exc.code == "timeout"
    assert elapsed < ELAPSED_LIMIT_S


def test_tls_handshake_drip_times_out_within_deadline(tmp_path):
    """Finding 1 (TLS): a ServerHello dripped 2 bytes at a time must fail.

    The server reads the ClientHello, then answers with a record header
    promising an 80-byte handshake record and drips only part of it. The
    client stays inside the handshake waiting for the rest of the
    record, so only a true total bound can stop it.
    """
    _, _, pin = generate_cert(tmp_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                hello = conn.recv(65536)
                if not hello:
                    return
                # TLS handshake record: type 22, version 3.3, 80 body
                # bytes promised. Only 60 ever arrive, then a hold.
                conn.sendall(b"\x16\x03\x03\x00\x50")
                for _ in range(30):
                    time.sleep(0.4)
                    try:
                        conn.sendall(b"AB")
                    except OSError:
                        break
                time.sleep(10)
            except OSError:
                pass
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.1)
    tls = TlsConfig(fingerprint_sha256=pin)
    client = Client("https", "127.0.0.1", port, ALLOW,
                    tls=tls, default_timeout_s=10)
    started = time.monotonic()
    with client:
        with pytest.raises(UpstreamError) as excinfo:
            client.request("GET", "/stat/sta", deadline_s=DEADLINE_S)
    elapsed = time.monotonic() - started
    assert excinfo.value.code == "timeout"
    assert elapsed < ELAPSED_LIMIT_S


def test_truncated_body_is_upstream_unexpected():
    """Finding 2: Content-Length 100 with 50 bytes then FIN must fail."""
    raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n"
           b"Connection: close\r\n\r\n" + b"y" * 50)
    routes = {("GET", "/s"): Canned(raw=raw)}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=5) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"


def test_tls_clean_close_mid_body_is_upstream_unexpected(tmp_path):
    """Finding 2 (TLS): a clean close_notify mid-body must still fail."""
    cert_path, key_path, pin = generate_cert(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        try:
            conn = server_context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        with conn:
            conn.settimeout(10)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n"
                             b"Connection: close\r\n\r\n" + b"q" * 50)
                try:
                    conn.unwrap()
                except OSError:
                    pass
            except OSError:
                pass
        try:
            raw.close()
        except OSError:
            pass
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.1)
    tls = TlsConfig(fingerprint_sha256=pin)
    client = Client("https", "127.0.0.1", port, ALLOW_S, tls=tls,
                    default_timeout_s=5)
    with client:
        with pytest.raises(UpstreamError) as excinfo:
            client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"


def test_rst_mid_body_is_upstream_unexpected():
    """An RST mid-body must fail, never return a short body as success."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n"
                         b"Connection: close\r\n\r\n" + b"z" * 10)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                             struct.pack("ii", 1, 0))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        listener.close()

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.1)
    with Client("http", "127.0.0.1", port, ALLOW_S,
                default_timeout_s=5) as client:
        with pytest.raises(UpstreamError) as excinfo:
            client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"


@pytest.mark.parametrize("value", [b"-5", b"abc", b"5, 6", b"", b"  "])
def test_invalid_content_length_is_upstream_unexpected(value):
    """Finding 4: the header must be decimal and >= 0, duplicates agreeing."""
    raw = (b"HTTP/1.1 200 OK\r\nContent-Length: " + value +
           b"\r\nConnection: close\r\n\r\nhello")
    routes = {("GET", "/s"): Canned(raw=raw)}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=5) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"


def test_agreeing_duplicate_content_length_succeeds():
    """Duplicates that agree describe one length, so the body is trusted."""
    raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 5\r\n"
           b"Connection: close\r\n\r\nhello")
    routes = {("GET", "/s"): Canned(raw=raw)}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=5) as client:
            response = client.request("GET", "/s", deadline_s=5)
    assert response.status == 200
    assert response.body == b"hello"


@pytest.mark.parametrize("status", [300, 304])
def test_non_redirect_3xx_reports_unexpected_status(status):
    """Finding 5: 300/304 are not redirects; the message must say so."""
    routes = {("GET", "/s"): Canned(status=status, body=b"x")}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=5) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"
    assert excinfo.value.status == status
    assert excinfo.value.message == f"unexpected status (status {status})"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_true_redirects_keep_the_redirect_wording(status):
    """301/302/303/307/308 stay 'unexpected redirect' with status kept."""
    routes = {("GET", "/s"): Canned(status=status, body=b"x")}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=5) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
    assert excinfo.value.code == "upstream_unexpected"
    assert excinfo.value.status == status
    assert excinfo.value.message == f"unexpected redirect (status {status})"


def test_http_scheme_rejects_insecure_tls():
    """Finding 6: TLS options, including insecure, need the https scheme."""
    with pytest.raises(ValueError):
        Client("http", "127.0.0.1", 80, ALLOW_S, tls=TlsConfig(insecure=True))


def test_fast_requests_leave_no_threads_behind():
    """A fast request must succeed and leave the watchdog threadless."""
    routes = {("GET", "/s"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        before = threading.active_count()
        with Client("http", "127.0.0.1", server.port, ALLOW_S,
                    default_timeout_s=10) as client:
            response = client.request("GET", "/s", deadline_s=5)
        after = threading.active_count()
    assert response.status == 200
    assert response.body == b"{}"
    assert after <= before
