"""Tests for core/http.py: allow-listing, redirects, caps, deadlines.

Total-deadline bounds live in ``TestTotalDeadline`` (merged from
``test_http_deadline.py``); response-framing re-checks live in
``TestResponseFramingRecheck`` (merged from ``test_http_recheck.py``).
"""

from __future__ import annotations

import socket
import ssl
import struct
import threading
import time

import pytest

from housesitter.core.http import (
    MAX_BODY_BYTES,
    Allow,
    Client,
    NotAllowed,
    TlsConfig,
    UpstreamError,
)

from .helpers import Canned, ScriptedServer, canned_handler, generate_cert

SECRET = "PLANTED-SECRET-login-body-1"
COOKIE_SECRET = "PLANTED-SECRET-cookie-2"
BODY_SECRET = "PLANTED-SECRET-response-body-3"

ALLOW = [Allow("GET", r"/stat/sta"), Allow("POST", r"/api/auth/login")]


def client_for(server, allow=ALLOW, **kwargs):
    return Client("http", "127.0.0.1", server.port, allow, **kwargs)


def closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_get_returns_status_headers_and_json_body():
    routes = {("GET", "/stat/sta"): Canned(body=b'{"clients": []}')}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            response = client.request("GET", "/stat/sta")
    assert response.status == 200
    assert response.json() == {"clients": []}


def test_method_matching_is_case_insensitive_and_tuples_work():
    routes = {("GET", "/stat/sta"): Canned(body=b"ok")}
    with ScriptedServer(canned_handler(routes)) as server:
        client = Client("http", "127.0.0.1", server.port, [("get", r"/stat/sta")])
        assert client.request("get", "/stat/sta").body == b"ok"
        assert server.requests[0].method == "GET"


def test_non_json_body_fails_as_upstream_unexpected():
    routes = {("GET", "/stat/sta"): Canned(body=b"not json {")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta").json()
    assert excinfo.value.code == "upstream_unexpected"


def test_cookies_are_kept_in_memory_and_sent_back():
    def handle(request):
        if request.target == "/api/auth/login":
            return Canned(body=b"{}", headers={"Set-Cookie": "sess=abc123; Path=/"})
        assert request.headers.get("cookie") == "sess=abc123"
        return Canned(body=b"{}")

    with ScriptedServer(handle) as server:
        with client_for(server) as client:
            client.request("POST", "/api/auth/login", body=b"{}")
            client.request("GET", "/stat/sta")
    assert len(server.requests) == 2


def test_csrf_token_is_sent_on_post_but_not_get():
    seen = {}

    def handle(request):
        seen[request.method] = request.headers.get("x-csrf-token")
        if request.method == "POST":
            return Canned(body=b"{}", headers={"X-CSRF-Token": "tok-1"})
        return Canned(body=b"{}")

    allow = [Allow("GET", r"/stat/sta"), Allow("POST", r"/api/auth/login"),
             Allow("POST", r"/api/auth/logout")]
    with ScriptedServer(handle) as server:
        with client_for(server, allow=allow) as client:
            client.request("POST", "/api/auth/login", body=b"{}")
            assert seen["POST"] is None
            client.request("GET", "/stat/sta")
            assert seen["GET"] is None
            client.request("POST", "/api/auth/logout", body=b"{}")
            assert seen["POST"] == "tok-1"
            client.clear_auth()
            client.request("POST", "/api/auth/logout", body=b"{}")
            assert seen["POST"] is None


@pytest.mark.parametrize(
    "method,target",
    [
        ("GET", "/stat/device-basic"),
        ("GET", "/rest/wlanconf"),
        ("DELETE", "/stat/sta"),
        ("POST", "/stat/sta"),
        ("GET", "/stat/sta?x=1"),
        ("GET", "/stat/sta%20x"),
        ("GET", "/stat/%2e%2e/x"),
        ("GET", "/a/../stat/sta"),
        ("GET", "//stat/sta"),
        ("GET", "/stat/sta/"),
        ("GET", "/stat/./sta"),
        ("GET", "http://x/stat/sta"),
        ("GET", "/stat/sta#frag"),
    ],
)
def test_disallowed_targets_never_open_a_socket(method, target):
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(NotAllowed):
                client.request(method, target)
        assert server.connections == 0
        assert server.requests == []


def test_query_is_allowed_only_when_the_entry_says_so():
    routes = {("GET", "/stat/sta?x=1"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        allow = [Allow("GET", r"/stat/sta", allow_query=True)]
        with client_for(server, allow=allow) as client:
            assert client.request("GET", "/stat/sta?x=1").status == 200
        assert server.requests[0].target == "/stat/sta?x=1"


def test_redirect_is_not_followed_and_becomes_upstream_unexpected():
    routes = {("GET", "/stat/sta"): Canned(status=302, headers={"Location": "/elsewhere"})}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_unexpected"
    assert excinfo.value.status == 302
    assert len(server.requests) == 1


def test_response_body_is_cut_off_at_8_mib():
    big = b"x" * (MAX_BODY_BYTES + 100)
    routes = {("GET", "/stat/sta"): Canned(body=big)}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_unexpected"
    assert "8 MiB" in excinfo.value.message


def test_garbage_response_is_upstream_unexpected():
    routes = {("GET", "/stat/sta"): Canned(raw=b"this is not http\r\n\r\n")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_unexpected"


def test_body_exactly_at_the_cap_is_returned():
    big = b"y" * MAX_BODY_BYTES
    routes = {("GET", "/stat/sta"): Canned(body=big)}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            assert len(client.request("GET", "/stat/sta").body) == MAX_BODY_BYTES


def test_slow_upstream_hits_the_deadline():
    routes = {("GET", "/stat/sta"): Canned(body=b"{}", sleep_s=5)}
    with ScriptedServer(canned_handler(routes)) as server:
        started = time.monotonic()
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", deadline_s=0.3)
        assert excinfo.value.code == "timeout"
        assert time.monotonic() - started < 3


def test_slow_drip_hits_the_deadline_mid_body():
    routes = {("GET", "/stat/sta"): Canned(body=b"z" * 100 * 1024, drip_s=0.05)}
    with ScriptedServer(canned_handler(routes)) as server:
        started = time.monotonic()
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", deadline_s=0.5)
        assert excinfo.value.code == "timeout"
        assert time.monotonic() - started < 4


def test_expired_deadline_sends_no_socket():
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", deadline_s=0)
        assert excinfo.value.code == "timeout"
        assert server.connections == 0


def test_connection_refused_is_unreachable():
    client = Client("http", "127.0.0.1", closed_port(), ALLOW, default_timeout_s=2)
    with pytest.raises(UpstreamError) as excinfo:
        client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_unreachable"


def test_auth_failure_status_is_returned_for_the_caller_to_map():
    routes = {("POST", "/api/auth/login"): Canned(status=401, body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            response = client.request("POST", "/api/auth/login", body=b"{}")
    assert response.status == 401


def test_error_text_never_contains_secrets_or_bodies():
    body = b'{"username": "u", "password": "PLANTED-SECRET-login-body-1"}'
    headers = {"Authorization": "Bearer PLANTED-SECRET-login-body-1"}
    routes = {
        ("POST", "/api/auth/login"): Canned(
            status=401,
            body=("login failed: " + BODY_SECRET).encode(),
            headers={"Set-Cookie": "sess=" + COOKIE_SECRET + "; Path=/"},
        ),
    }
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            response = client.request(
                "POST", "/api/auth/login", body=body, headers=headers
            )
            assert response.status == 401
            with pytest.raises(UpstreamError) as excinfo:
                client.request("POST", "/api/auth/login", body=body,
                               headers=headers, deadline_s=0)
    assert SECRET not in str(excinfo.value)
    # A refused connection while secrets are in play stays silent too.
    refused = Client("http", "127.0.0.1", closed_port(), ALLOW, default_timeout_s=2)
    with pytest.raises(UpstreamError) as excinfo:
        refused.request("POST", "/api/auth/login", body=body, headers=headers)
    assert SECRET not in str(excinfo.value)
    assert BODY_SECRET not in str(excinfo.value)
    assert COOKIE_SECRET not in str(excinfo.value)


def test_not_allowed_hides_query_values():
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(NotAllowed) as excinfo:
                client.request("GET", "/stat/sta?password=" + SECRET)
    assert SECRET not in str(excinfo.value)


def test_oversized_request_body_is_refused():
    with ScriptedServer(canned_handler({})) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("POST", "/api/auth/login", body=b"q" * (MAX_BODY_BYTES + 1))
    assert excinfo.value.code == "upstream_unexpected"
    assert server.connections == 0


def test_percent_refused_even_when_pattern_would_match():
    # M1: the allow pattern /api/.* would match, so only the always-on
    # %-refusal stops this target. Removing that refusal must fail here.
    allow = [Allow("GET", r"/api/.*", allow_query=True)]
    routes = {("GET", "/api/x%20y"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server, allow=allow) as client:
            with pytest.raises(NotAllowed):
                client.request("GET", "/api/x%20y")
        assert server.connections == 0
        assert server.requests == []


@pytest.mark.parametrize("header", ["Host", "host", "HOST", "Content-Length",
                                    "content-length", "CONTENT-LENGTH"])
def test_caller_supplied_host_and_content_length_are_refused(header):
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(ValueError):
                client.request("GET", "/stat/sta", headers={header: "x-example"})
        assert server.connections == 0
        assert server.requests == []


@pytest.mark.parametrize("kwargs", [
    {"fingerprint_sha256": "ab" * 32},
    {"ca_file": "/tmp/cert.pem"},
    {"server_hostname": "x-example"},
])
def test_http_scheme_rejects_tls_config_at_construction(kwargs):
    from housesitter.core.http import TlsConfig

    with pytest.raises(ValueError):
        Client("http", "127.0.0.1", 8443, ALLOW, tls=TlsConfig(**kwargs))


def test_https_pin_against_plain_http_is_upstream_tls():
    from housesitter.core.http import TlsConfig

    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes)) as server:
        client = Client("https", "127.0.0.1", server.port, ALLOW,
                        tls=TlsConfig(fingerprint_sha256="ab" * 32),
                        default_timeout_s=5)
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_tls"
    assert server.requests == []


def test_https_pin_against_closed_port_is_unreachable():
    from housesitter.core.http import TlsConfig

    client = Client("https", "127.0.0.1", closed_port(), ALLOW,
                    tls=TlsConfig(fingerprint_sha256="ab" * 32),
                    default_timeout_s=2)
    with pytest.raises(UpstreamError) as excinfo:
        client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_unreachable"


PLANTED = "PLANTED-SECRET-notallowed-4"


@pytest.mark.parametrize("method,target", [
    ("GET", "password=" + PLANTED),          # not an absolute path
    ("GET", "/" + PLANTED + " with space"),  # control/space characters
    ("GET", "/stat/sta#" + PLANTED),         # fragment
    ("GET", "/stat/sta%" + PLANTED),         # percent escape
    ("GET", "/a/../stat/sta"),               # dot segment
    ("GET", "//stat/sta"),                   # empty segment
    ("GET", "/no/such/path?" + PLANTED),     # query the entry disallows
])
def test_not_allowed_never_echoes_the_target(method, target):
    with ScriptedServer(canned_handler({})) as server:
        with client_for(server) as client:
            with pytest.raises(NotAllowed) as excinfo:
                client.request(method, target)
        assert server.connections == 0
    assert PLANTED not in str(excinfo.value)
    assert PLANTED not in repr(excinfo.value)


def _walk_chain(exc):
    seen = []
    node = exc
    while node is not None:
        seen.append(node)
        node = node.__cause__ or node.__context__
    return seen


def test_upstream_error_chain_carries_no_planted_text():
    body_secret = "PLANTED-SECRET-chain-body-6"
    header_secret = "PLANTED-SECRET-chain-header-6"
    resp_secret = "PLANTED-SECRET-chain-resp-6"
    cookie_secret = "PLANTED-SECRET-chain-cookie-6"
    csrf_secret = "PLANTED-SECRET-chain-csrf-6"
    planted = [body_secret, header_secret, resp_secret, cookie_secret,
               csrf_secret]
    body = ('{"u": "u", "p": "' + body_secret + '"}').encode()
    headers = {"Authorization": "Bearer " + header_secret}

    def assert_clean(exc):
        assert exc.__cause__ is None
        assert exc.__context__ is None
        for node in _walk_chain(exc):
            for blob in (str(node), repr(node)):
                for secret in planted:
                    assert secret not in blob

    # 1. Timeout with secrets in play.
    routes = {("POST", "/api/auth/login"): Canned(body=b"{}", sleep_s=5)}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("POST", "/api/auth/login", body=body,
                               headers=headers, deadline_s=0.3)
    assert_clean(excinfo.value)

    # 2. Unreachable with secrets in play.
    refused = Client("http", "127.0.0.1", closed_port(), ALLOW,
                     default_timeout_s=2)
    with pytest.raises(UpstreamError) as excinfo:
        refused.request("POST", "/api/auth/login", body=body, headers=headers)
    assert_clean(excinfo.value)

    # 3. Garbage status line carrying a secret stays off the chain.
    routes = {("GET", "/stat/sta"): Canned(
        raw=b"not http at all " + resp_secret.encode() + b"\r\n\r\n")}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", body=body, headers=headers)
    assert_clean(excinfo.value)

    # 4. Redirect carrying secrets in Location and body.
    routes = {("GET", "/stat/sta"): Canned(
        status=302,
        body=("redir " + resp_secret).encode(),
        headers={"Location": "http://x-example/?t=" + cookie_secret})}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", headers=headers)
    assert_clean(excinfo.value)

    # 5. Oversize response body holding a secret.
    big = b"x" * (MAX_BODY_BYTES + 64) + resp_secret.encode()
    with ScriptedServer(canned_handler({("GET", "/stat/sta"): Canned(body=big)})) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert_clean(excinfo.value)

    # 6. Oversize request body holding a secret.
    with ScriptedServer(canned_handler({})) as server:
        with client_for(server) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("POST", "/api/auth/login",
                               body=b"p=" + body_secret.encode()
                               + b"z" * (MAX_BODY_BYTES + 1))
    assert_clean(excinfo.value)

    # 7. Stored cookies/CSRF plus an expired deadline.
    routes = {("POST", "/api/auth/login"): Canned(
        body=("boom " + resp_secret).encode(),
        headers={"Set-Cookie": "sess=" + cookie_secret + "; Path=/",
                 "X-CSRF-Token": csrf_secret})}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            client.request("POST", "/api/auth/login", body=body, headers=headers)
            with pytest.raises(UpstreamError) as excinfo:
                client.request("POST", "/api/auth/login", body=body,
                               headers=headers, deadline_s=0)
    assert_clean(excinfo.value)

    # 8. JSON decode failure on a hostile body.
    routes = {("GET", "/stat/sta"): Canned(
        body=("not json {" + resp_secret).encode())}
    with ScriptedServer(canned_handler(routes)) as server:
        with client_for(server) as client:
            response = client.request("GET", "/stat/sta")
            with pytest.raises(UpstreamError) as excinfo:
                response.json()
    assert_clean(excinfo.value)


def test_set_cookie_with_crlf_is_never_stored_or_sent():
    # M15: dropping the CR/LF injection guard must fail here. A hostile
    # value is planted straight into stored-auth handling, then a second
    # request proves nothing hostile reaches the wire.
    with ScriptedServer(canned_handler({("GET", "/stat/sta"): Canned(body=b"{}")})) as server:
        with client_for(server) as client:
            client._remember_auth(
                [("Set-Cookie", "sess=abc\r\nX-Evil: 1; Path=/")])
            client.request("GET", "/stat/sta")
        assert server.requests[-1].headers.get("cookie") is None


def test_csrf_token_with_crlf_is_never_stored():
    with ScriptedServer(canned_handler({("GET", "/stat/sta"): Canned(body=b"{}")})) as server:
        with client_for(server) as client:
            client._remember_auth([("X-CSRF-Token", "tok\r\nX-Evil: 2")])
            assert client._csrf_token is None


# -- total deadline (merged from test_http_deadline.py) -------------------------
#
# Every test here sets ``deadline_s`` and asserts the deadline is a total
# bound: success must arrive with the body intact, and a stall must fail
# as ``timeout`` within the deadline plus a small tolerance.


DEADLINE_ALLOW = [Allow("GET", r"/stat/sta")]

DEADLINE_BODY = b'{"clients": []}'

DEADLINE_TOLERANCE_S = 2.0


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


class TestTotalDeadline:
    def test_deadline_success_keepalive_content_length(self):
        port = _hold_open_server(DEADLINE_BODY)
        client = Client("http", "127.0.0.1", port, DEADLINE_ALLOW,
                        default_timeout_s=10)
        with client:
            response = client.request("GET", "/stat/sta", deadline_s=5)
        assert response.status == 200
        assert response.body == DEADLINE_BODY

    def test_deadline_success_connection_close(self):
        routes = {("GET", "/stat/sta"): Canned(body=DEADLINE_BODY)}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, DEADLINE_ALLOW,
                        default_timeout_s=10) as client:
                response = client.request("GET", "/stat/sta", deadline_s=5)
        assert response.status == 200
        assert response.body == DEADLINE_BODY

    def test_deadline_success_keepalive_chunked(self):
        port = _hold_open_server(DEADLINE_BODY, use_chunked=True)
        client = Client("http", "127.0.0.1", port, DEADLINE_ALLOW,
                        default_timeout_s=10)
        with client:
            response = client.request("GET", "/stat/sta", deadline_s=5)
        assert response.status == 200
        assert response.body == DEADLINE_BODY

    def test_deadline_success_tls_keepalive(self, tmp_path):
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
                        b"Content-Length: " + str(len(DEADLINE_BODY)).encode() + b"\r\n"
                        b"Connection: keep-alive\r\n"
                        b"\r\n"
                    ) + DEADLINE_BODY
                    conn.sendall(head)
                    time.sleep(10)
                except OSError:
                    pass
            listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        tls = TlsConfig(fingerprint_sha256=pin)
        client = Client("https", "127.0.0.1", port, DEADLINE_ALLOW, tls=tls,
                        default_timeout_s=10)
        with client:
            response = client.request("GET", "/stat/sta", deadline_s=5)
        assert response.status == 200
        assert response.body == DEADLINE_BODY

    def test_stalled_body_times_out_within_deadline(self):
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
        client = Client("http", "127.0.0.1", port, DEADLINE_ALLOW,
                        default_timeout_s=10)
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", deadline_s=deadline)
        elapsed = time.monotonic() - started
        assert excinfo.value.code == "timeout"
        assert elapsed < deadline + DEADLINE_TOLERANCE_S

    def test_slow_handshake_plus_slow_response_stays_inside_deadline(self, tmp_path):
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
        client = Client("https", "127.0.0.1", port, DEADLINE_ALLOW, tls=tls,
                        default_timeout_s=10)
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta", deadline_s=deadline)
        elapsed = time.monotonic() - started
        assert excinfo.value.code == "timeout"
        assert elapsed < deadline + DEADLINE_TOLERANCE_S


# -- response framing re-checks (merged from test_http_recheck.py) ------------
#
# Every test here pins one finding: a drip that must fail within
# ``deadline + 0.5`` s, a truncated or malformed body that must fail as
# ``upstream_unexpected``, and the smaller contract fixes (304 wording,
# ``http`` + ``insecure``). Drip servers use raw sockets so each byte can
# be paced; nothing here touches the network beyond 127.0.0.1.


RECHECK_ALLOW = [Allow("GET", r"/stat/sta"), Allow("GET", r"/s")]

RECHECK_DEADLINE_S = 1.0
# A total bound must fire within the deadline plus a small margin for
# wake-up and error mapping, never a multiple of the deadline.
RECHECK_ELAPSED_LIMIT_S = RECHECK_DEADLINE_S + 0.5

RECHECK_ALLOW_S = [Allow("GET", r"/s")]


def _recheck_serve_raw(head: bytes, drips: list, drip_s: float,
                       holder: list) -> None:
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
                conn.sendall(head)
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


def _recheck_drip_request(head: bytes, drips: list, drip_s: float = 0.4):
    holder: list = []
    _recheck_serve_raw(head, drips, drip_s, holder)
    time.sleep(0.1)
    client = Client("http", "127.0.0.1", holder[0], RECHECK_ALLOW_S,
                    default_timeout_s=10)
    started = time.monotonic()
    try:
        with client:
            client.request("GET", "/s", deadline_s=RECHECK_DEADLINE_S)
    except UpstreamError as exc:
        return exc, time.monotonic() - started
    raise AssertionError("drip-feed succeeded, deadline was not a total bound")


class TestResponseFramingRecheck:
    def test_body_drip_times_out_within_deadline(self):
        """Finding 1: 1 byte every 0.4 s with deadline 1.0 must time out fast."""
        head = (b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n"
                b"Connection: close\r\n\r\n")
        exc, elapsed = _recheck_drip_request(head, [b"x"] * 20)
        assert exc.code == "timeout"
        assert elapsed < RECHECK_ELAPSED_LIMIT_S

    def test_chunked_body_drip_times_out_within_deadline(self):
        """Finding 1 (chunked): one 1-byte chunk every 0.4 s must time out."""
        head = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n")
        drips = [b"1\r\nx\r\n"] * 10 + [b"0\r\n\r\n"]
        exc, elapsed = _recheck_drip_request(head, drips)
        assert exc.code == "timeout"
        assert elapsed < RECHECK_ELAPSED_LIMIT_S

    def test_chunk_header_drip_times_out_within_deadline(self):
        """A chunk-size line dripped 1 byte every 0.4 s must time out."""
        head = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n")
        drips = [bytes([byte]) for byte in b"5\r\nhello\r\n0\r\n\r\n"]
        exc, elapsed = _recheck_drip_request(head, drips)
        assert exc.code == "timeout"
        assert elapsed < RECHECK_ELAPSED_LIMIT_S

    def test_header_drip_times_out_within_deadline(self):
        """Finding 3: headers dripped 2 bytes every 0.4 s must time out."""
        full = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        drips = [full[index:index + 2] for index in range(0, len(full), 2)]
        exc, elapsed = _recheck_drip_request(b"", drips)
        assert exc.code == "timeout"
        assert elapsed < RECHECK_ELAPSED_LIMIT_S

    def test_tls_handshake_drip_times_out_within_deadline(self, tmp_path):
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
        client = Client("https", "127.0.0.1", port, RECHECK_ALLOW,
                        tls=tls, default_timeout_s=10)
        started = time.monotonic()
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta",
                               deadline_s=RECHECK_DEADLINE_S)
        elapsed = time.monotonic() - started
        assert excinfo.value.code == "timeout"
        assert elapsed < RECHECK_ELAPSED_LIMIT_S

    def test_truncated_body_is_upstream_unexpected(self):
        """Finding 2: Content-Length 100 with 50 bytes then FIN must fail."""
        raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n"
               b"Connection: close\r\n\r\n" + b"y" * 50)
        routes = {("GET", "/s"): Canned(raw=raw)}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=5) as client:
                with pytest.raises(UpstreamError) as excinfo:
                    client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"

    def test_tls_clean_close_mid_body_is_upstream_unexpected(self, tmp_path):
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
        client = Client("https", "127.0.0.1", port, RECHECK_ALLOW_S, tls=tls,
                        default_timeout_s=5)
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"

    def test_rst_mid_body_is_upstream_unexpected(self):
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
        with Client("http", "127.0.0.1", port, RECHECK_ALLOW_S,
                    default_timeout_s=5) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"

    @pytest.mark.parametrize("value", [b"-5", b"abc", b"5, 6", b"", b"  "])
    def test_invalid_content_length_is_upstream_unexpected(self, value):
        """Finding 4: the header must be decimal and >= 0, duplicates agreeing."""
        raw = (b"HTTP/1.1 200 OK\r\nContent-Length: " + value +
               b"\r\nConnection: close\r\n\r\nhello")
        routes = {("GET", "/s"): Canned(raw=raw)}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=5) as client:
                with pytest.raises(UpstreamError) as excinfo:
                    client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"

    def test_agreeing_duplicate_content_length_succeeds(self):
        """Duplicates that agree describe one length, so the body is trusted."""
        raw = (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 5\r\n"
               b"Connection: close\r\n\r\nhello")
        routes = {("GET", "/s"): Canned(raw=raw)}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=5) as client:
                response = client.request("GET", "/s", deadline_s=5)
        assert response.status == 200
        assert response.body == b"hello"

    @pytest.mark.parametrize("status", [300, 304])
    def test_non_redirect_3xx_reports_unexpected_status(self, status):
        """Finding 5: 300/304 are not redirects; the message must say so."""
        routes = {("GET", "/s"): Canned(status=status, body=b"x")}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=5) as client:
                with pytest.raises(UpstreamError) as excinfo:
                    client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"
        assert excinfo.value.status == status
        assert excinfo.value.message == f"unexpected status (status {status})"

    @pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
    def test_true_redirects_keep_the_redirect_wording(self, status):
        """301/302/303/307/308 stay 'unexpected redirect' with status kept."""
        routes = {("GET", "/s"): Canned(status=status, body=b"x")}
        with ScriptedServer(canned_handler(routes)) as server:
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=5) as client:
                with pytest.raises(UpstreamError) as excinfo:
                    client.request("GET", "/s", deadline_s=5)
        assert excinfo.value.code == "upstream_unexpected"
        assert excinfo.value.status == status
        assert excinfo.value.message == f"unexpected redirect (status {status})"

    def test_http_scheme_rejects_insecure_tls(self):
        """Finding 6: TLS options, including insecure, need the https scheme."""
        with pytest.raises(ValueError):
            Client("http", "127.0.0.1", 80, RECHECK_ALLOW_S,
                   tls=TlsConfig(insecure=True))

    def test_fast_requests_leave_no_threads_behind(self):
        """A fast request must succeed and leave the watchdog threadless."""
        routes = {("GET", "/s"): Canned(body=b"{}")}
        with ScriptedServer(canned_handler(routes)) as server:
            before = set(threading.enumerate())
            with Client("http", "127.0.0.1", server.port, RECHECK_ALLOW_S,
                        default_timeout_s=10) as client:
                response = client.request("GET", "/s", deadline_s=5)
            # Compare thread objects, not a bare count: the test server's own
            # per-connection handler thread may still be closing its socket
            # here, which made a count comparison flaky. The watchdog is a
            # threading.Timer, so no new live Timer may remain.
            leftover = [
                thread for thread in threading.enumerate()
                if thread not in before and isinstance(thread, threading.Timer)
            ]
        assert response.status == 200
        assert response.body == b"{}"
        assert leftover == []
