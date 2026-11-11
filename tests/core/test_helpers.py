"""Tests for the test helper ``ScriptedServer`` itself.

Many tests read ``server.requests``, ``server.peer_bytes`` and
``server.connections`` right after ``close()``. These tests pin that
those are final by then, and that ``close()`` is fast, because the
conformance harness and most core tests open and close a server per case.
"""

from __future__ import annotations

import socket
import threading
import time

from .helpers import Canned, ScriptedServer, canned_handler

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
    with ScriptedServer(OK) as server:
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
        time.sleep(0.6)  # longer than the old 0.5 s accept timeout
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
