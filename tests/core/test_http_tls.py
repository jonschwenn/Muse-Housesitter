"""TLS tests for core/http.py: pins, CA files, and insecure mode.

Every test mints its own throwaway self-signed cert with the ``openssl``
CLI and serves it from an in-process server on 127.0.0.1. No key
material is committed. When ``openssl`` is not on PATH, these tests
skip with a clear reason, or fail when ``HOUSESITTER_REQUIRE_TEST_TOOLS=1``
(set in CI).
"""

from __future__ import annotations

import pytest

from housesitter.core.http import (
    Allow,
    Client,
    TlsConfig,
    UpstreamError,
)

from .helpers import Canned, ScriptedServer, canned_handler, generate_cert

ALLOW = [Allow("GET", r"/stat/sta")]


@pytest.fixture
def cert(tmp_path):
    return generate_cert(tmp_path)


def tls_client(server, tls):
    return Client("https", "127.0.0.1", server.port, ALLOW, tls=tls,
                  default_timeout_s=5)


def test_system_verification_rejects_self_signed(cert):
    cert_path, _, _ = cert
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=cert[1]) as server:
        with tls_client(server, TlsConfig()) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_tls"


def test_ca_file_verifies_the_throwaway_cert(cert):
    cert_path, key_path, _ = cert
    routes = {("GET", "/stat/sta"): Canned(body=b'{"ok": true}')}
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=key_path) as server:
        with tls_client(server, TlsConfig(ca_file=cert_path)) as client:
            response = client.request("GET", "/stat/sta")
    assert response.json() == {"ok": True}


def test_ca_file_with_wrong_server_hostname_fails(cert):
    cert_path, key_path, _ = cert
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=key_path) as server:
        tls = TlsConfig(ca_file=cert_path, server_hostname="wrong-host-example")
        with tls_client(server, tls) as client:
            with pytest.raises(UpstreamError) as excinfo:
                client.request("GET", "/stat/sta")
    assert excinfo.value.code == "upstream_tls"


def test_correct_pin_sends_the_request(cert):
    cert_path, key_path, pin = cert
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=key_path) as server:
        tls = TlsConfig(fingerprint_sha256=":".join(
            pin[i:i + 2] for i in range(0, len(pin), 2)).upper())
        with tls_client(server, tls) as client:
            assert client.request("GET", "/stat/sta").status == 200
    assert [request.target for request in server.requests] == ["/stat/sta"]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_wrong_pin_sends_zero_request_bytes(cert, method):
    cert_path, key_path, pin = cert
    wrong = ("0" if pin[0] != "0" else "1") + pin[1:]
    routes = {}
    allow = [Allow("GET", r"/stat/sta"), Allow("POST", r"/api/auth/login")]
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=key_path) as server:
        tls = TlsConfig(fingerprint_sha256=wrong)
        client = Client("https", "127.0.0.1", server.port, allow, tls=tls,
                        default_timeout_s=5)
        with client:
            with pytest.raises(UpstreamError) as excinfo:
                if method == "GET":
                    client.request("GET", "/stat/sta")
                else:
                    client.request(
                        "POST", "/api/auth/login",
                        headers={"Authorization": "Bearer PLANTED-SECRET-pin-1"},
                        body=b'{"password": "PLANTED-SECRET-pin-2"}',
                    )
    assert excinfo.value.code == "upstream_tls"
    assert server.requests == []
    assert server.peer_bytes and all(data == b"" for data in server.peer_bytes)


def test_insecure_mode_is_explicit_and_works(cert):
    cert_path, key_path, _ = cert
    routes = {("GET", "/stat/sta"): Canned(body=b"{}")}
    with ScriptedServer(canned_handler(routes), tls_cert=cert_path,
                        tls_key=key_path) as server:
        with tls_client(server, TlsConfig(insecure=True)) as client:
            assert client.request("GET", "/stat/sta").status == 200


def test_insecure_is_off_by_default():
    assert TlsConfig().insecure is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"insecure": True, "fingerprint_sha256": "ab" * 32},
        {"insecure": True, "ca_file": "/tmp/cert.pem"},
        {"fingerprint_sha256": "not-hex"},
        {"fingerprint_sha256": "ab" * 31},
        {"server_hostname": "x", "insecure": True},
        {"server_hostname": "x", "fingerprint_sha256": "ab" * 32},
    ],
)
def test_invalid_tls_configs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        TlsConfig(**kwargs)
