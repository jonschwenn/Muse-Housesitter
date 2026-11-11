"""Tests for the test helper ``ScriptedServer`` itself.

Many tests read ``server.requests``, ``server.peer_bytes`` and
``server.connections`` right after ``close()``. These tests pin that
those are final by then, and that ``close()`` is fast, because the
conformance harness, the module tests and the core HTTP tests open and
close a server per case.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from .helpers import Canned, ScriptedServer, canned_handler, generate_cert

OK = canned_handler({}, default=Canned(body=b"ok"))


def _wait_for(predicate, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.005)


def test_close_of_an_idle_server_is_fast():
    for _ in range(3):
        server = ScriptedServer(OK)
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 0.2


def test_close_after_a_request_is_fast():
    server = ScriptedServer(OK)
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n")
        while sock.recv(65536):
            pass
    started = time.monotonic()
    server.close()
    assert time.monotonic() - started < 0.2


def test_records_are_final_when_close_returns():
    # The client is still sending its body when close() starts. close()
    # must wait for that connection, so the request is fully recorded.
    server = ScriptedServer(OK)
    head = b"POST /late HTTP/1.1\r\nHost: t\r\nContent-Length: 10\r\n\r\n"
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(head + b"01234")
        _wait_for(lambda: server.connections == 1 and server.peer_bytes)
        closer = threading.Thread(target=server.close)
        closer.start()
        # Longer than the old 0.5 s accept timeout, shorter than the
        # server's 2 s per-recv timeout.
        time.sleep(0.6)
        sock.sendall(b"56789")
        closer.join(timeout=10)
        assert not closer.is_alive()
        assert server.connections == 1
        assert server.peer_bytes == [head + b"0123456789"]
        assert [(r.method, r.target, r.body) for r in server.requests] == [
            ("POST", "/late", b"0123456789")
        ]


def _close_during_slow_response(canned):
    server = ScriptedServer(canned_handler({}, default=canned))
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
        sock.sendall(b"GET /slow HTTP/1.1\r\nHost: t\r\n\r\n")
        _wait_for(lambda: server.requests)
        started = time.monotonic()
        server.close()
        elapsed = time.monotonic() - started
    return elapsed


def test_close_does_not_wait_out_a_canned_sleep():
    assert _close_during_slow_response(Canned(body=b"x", sleep_s=5.0)) < 1.0


def test_close_does_not_wait_out_a_canned_drip():
    canned = Canned(body=b"x" * 4096, drip_s=5.0)
    assert _close_during_slow_response(canned) < 1.0


def test_close_is_bounded_when_a_tls_client_never_handshakes(tmp_path):
    # A plain TCP client that never sends a ClientHello must not keep a
    # connection thread alive: the handshake times out like a recv does.
    cert_path, key_path, _ = generate_cert(tmp_path)
    server = ScriptedServer(OK, tls_cert=cert_path, tls_key=key_path)
    with socket.create_connection(("127.0.0.1", server.port), timeout=5):
        _wait_for(lambda: server.connections == 1)
        started = time.monotonic()
        server.close()
        elapsed = time.monotonic() - started
    assert elapsed < 4.0
    assert server.peer_bytes == []
    assert server.requests == []


def test_close_raises_when_a_connection_thread_is_stuck(monkeypatch):
    # close() must not return partial records silently: a connection
    # thread still running after its join is an error.
    release = threading.Event()

    def blocking(request):
        release.wait(10)
        return Canned(body=b"ok")

    monkeypatch.setattr(ScriptedServer, "join_timeout_s", 0.1)
    server = ScriptedServer(blocking)
    try:
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(b"GET /x HTTP/1.1\r\nHost: t\r\n\r\n")
            _wait_for(lambda: server.requests)
            with pytest.raises(AssertionError, match="still running"):
                server.close()
    finally:
        release.set()
