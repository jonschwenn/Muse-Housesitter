"""Deadline tests for core/http.py (findings 1 and 3).

Every test here sets ``deadline_s`` and asserts the deadline is a total
bound: success must arrive with the body intact, and a stall must fail
as ``timeout`` within the deadline plus a small tolerance.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest

from housesitter.core.http import Allow, Client, TlsConfig, UpstreamError

from .helpers import generate_cert

ALLOW = [Allow("GET", r"/stat/sta")]

BODY = b'{"clients": []}'

TOLERANCE_S = 2.0


def _hold_open_server(body: bytes, use_chunked: bool = False):
    """Serve one response with headers+body, then hold the socket open.

    Returns ``(port, thread)``. Holding the connection open is the point:
    the body is already in the client's user-space buffer, so a client
    that waits for readability instead of just reading will stall.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
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
                if use_chunked:
                    head = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"Connection: keep-alive\r\n"
                        b"\r\n"
                        + (f"{len(body):x}\r\n").encode()
                        + body
                        + b"\r\n0\r\n\r\n"
                    )
                else:
                    head = (
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                        b"Connection: keep-alive\r\n"
                        b"\r\n"
                    ) + body
                conn.sendall(head)
                time.sleep(10)
            except OSError:
                pass
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port


def test_deadline_success_keepalive_content_length():
    port = _hold_open_server(BODY)
    client = Client("http", "127.0.0.1", port, ALLOW, default_timeout_s=10)
    with client:
        response = client.request("GET", "/stat/sta", deadline_s=5)
    assert response.status == 200
    assert response.body == BODY


def test_deadline_success_connection_close():
    from .helpers import Canned, ScriptedServer, canned_handler

    routes = {("GET", "/stat/sta"): Canned(body=BODY)}
    with ScriptedServer(canned_handler(routes)) as server:
        with Client("http", "127.0.0.1", server.port, ALLOW,
                    default_timeout_s=10) as client:
            response = client.request("GET", "/stat/sta", deadline_s=5)
    assert response.status == 200
    assert response.body == BODY


def test_deadline_success_keepalive_chunked():
    port = _hold_open_server(BODY, use_chunked=True)
    client = Client("http", "127.0.0.1", port, ALLOW, default_timeout_s=10)
    with client:
        response = client.request("GET", "/stat/sta", deadline_s=5)
    assert response.status == 200
    assert response.body == BODY


def test_deadline_success_tls_keepalive(tmp_path):
    cert_path, key_path, pin = generate_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        try:
            conn = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
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
                head = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(BODY)).encode() + b"\r\n"
                    b"Connection: keep-alive\r\n"
                    b"\r\n"
                ) + BODY
                conn.sendall(head)
                time.sleep(10)
            except OSError:
                pass
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    tls = TlsConfig(fingerprint_sha256=pin)
    client = Client("https", "127.0.0.1", port, ALLOW, tls=tls,
                    default_timeout_s=10)
    with client:
        response = client.request("GET", "/stat/sta", deadline_s=5)
    assert response.status == 200
    assert response.body == BODY


def test_stalled_body_times_out_within_deadline():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            conn, _ = listener.accept()
        except OSError:
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
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n"
                    b"Connection: close\r\n\r\n"
                )
                time.sleep(10)
            except OSError:
                pass
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = 1.0
    started = time.monotonic()
    client = Client("http", "127.0.0.1", port, ALLOW, default_timeout_s=10)
    with client:
        with pytest.raises(UpstreamError) as excinfo:
            client.request("GET", "/stat/sta", deadline_s=deadline)
    elapsed = time.monotonic() - started
    assert excinfo.value.code == "timeout"
    assert elapsed < deadline + TOLERANCE_S


def test_slow_handshake_plus_slow_response_stays_inside_deadline(tmp_path):
    """Handshake and response-header stalls share one deadline budget."""
    cert_path, key_path, pin = generate_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            raw, _ = listener.accept()
        except OSError:
            return
        time.sleep(0.6)  # burns the connect-stage budget
        try:
            conn = context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
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
                time.sleep(0.6)  # burns the getresponse-stage budget
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                    b"Connection: close\r\n\r\n{}"
                )
            except OSError:
                pass
        listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = 1.0
    started = time.monotonic()
    tls = TlsConfig(fingerprint_sha256=pin)
    client = Client("https", "127.0.0.1", port, ALLOW, tls=tls,
                    default_timeout_s=10)
    with client:
        with pytest.raises(UpstreamError) as excinfo:
            client.request("GET", "/stat/sta", deadline_s=deadline)
    elapsed = time.monotonic() - started
    assert excinfo.value.code == "timeout"
    assert elapsed < deadline + TOLERANCE_S
