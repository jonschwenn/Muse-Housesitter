"""DEV/TEST ONLY fake UniFi OS controller. Never packaged, never deployed.

A stdlib HTTPS server that plays a UniFi OS controller from the invented
fixtures in ``tests/fixtures/unifi_wifi/``. It exists so the README guide
can be tested end to end in a container:

1. Make a throwaway self-signed certificate (the caller generates it;
   it never leaves /tmp)::

     openssl req -x509 -newkey rsa:2048 -nodes \\
       -keyout /tmp/fake-key.pem -out /tmp/fake-cert.pem \\
       -days 1 -subj "/CN=127.0.0.1"

2. Run this script::

     python3 scripts/fake_unifi.py \\
       --cert /tmp/fake-cert.pem --key /tmp/fake-key.pem

3. Point ``install/config.example.json`` at ``https://127.0.0.1:8443``
   with the printed ``fingerprint_sha256``, start ``housesitter serve``
   or take a snapshot, and check the output against the fixtures.

Behaviour notes:

* Only the UniFi OS routes are served (``/api/auth/*`` plus
  ``/proxy/network/api/s/<site>/stat/*`` for any site name), following
  the typical case in ``tests/fixtures/unifi_wifi/conformance.json``.
* Login accepts any non-empty username and secret and answers 401
  otherwise. The secret is never logged, echoed, or stored: request
  lines log only the method, path, and numeric status.
* Each login mints a fresh random cookie and ``X-CSRF-Token``, like a
  real controller (see ``core/http.py`` for how the client keeps them).
* ``GET /`` answers 200: flavour detection probes it before logging in.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import secrets
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE_DIR = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "unifi_wifi"
)

_SITE_STAT_RE = re.compile(
    r"^/proxy/network/api/s/([A-Za-z0-9_-]{1,64})/stat/(sysinfo|device|sta)$"
)
_ENDPOINT_FILES = {
    "sysinfo": "unifi_os_sysinfo.json",
    "device": "unifi_os_device.json",
    "sta": "unifi_os_sta.json",
}

MAX_BODY_BYTES = 1024 * 1024


def _fixture(name):
    # type: (str) -> bytes
    with open(FIXTURE_DIR / name, "rb") as handle:
        return handle.read()


def leaf_fingerprint(cert_path):
    # type: (str) -> str
    """SHA-256 of the leaf certificate, colon-separated uppercase hex."""
    with open(cert_path, "rb") as handle:
        pem = handle.read().decode("ascii")
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-unifi"
    sys_version = ""

    def log_message(self, fmt, *args):
        # Request lines only: never bodies, cookies, or tokens.
        sys.stderr.write(f"fake_unifi: {fmt % args}\n")

    def _send_json(self, payload, status=200, headers=()):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return None

    def do_GET(self):
        if self.path == "/":
            self._send_json({})
            return
        match = _SITE_STAT_RE.match(self.path or "")
        if match is not None:
            name = _ENDPOINT_FILES[match.group(2)]
            self._send_json(json.loads(_fixture(name).decode("utf-8")))
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path == "/api/auth/login":
            body = self._read_json_body()
            name = body.get("username") if isinstance(body, dict) else ""
            code = body.get("password") if isinstance(body, dict) else ""
            if not isinstance(name, str) or not name:
                self._send_json({"meta": {"rc": "error"}}, status=401)
                return
            if not isinstance(code, str) or not code:
                self._send_json({"meta": {"rc": "error"}}, status=401)
                return
            token = secrets.token_hex(16)
            self._send_json(
                json.loads(_fixture("unifi_os_login.json").decode("utf-8")),
                headers=(
                    ("Set-Cookie", f"TOKEN={token}; Path=/; HttpOnly; Secure"),
                    ("X-CSRF-Token", token),
                ),
            )
            return
        if self.path == "/api/auth/logout":
            length = self.headers.get("Content-Length")
            if length:
                try:
                    if int(length) > MAX_BODY_BYTES:
                        self._send_json({"error": "too large"}, status=413)
                        return
                except ValueError:
                    pass
            self._send_json({})
            return
        self._send_json({"error": "not found"}, status=404)

    def do_PUT(self):
        self._send_json({"error": "method not allowed"}, status=405)

    do_DELETE = do_PUT
    do_PATCH = do_PUT


def build_server(cert_path, key_path, bind, port):
    # type: (str, str, str, int) -> ThreadingHTTPServer
    # Test data only, and login accepts any secret: never reachable off-host.
    if not ipaddress.ip_address(bind).is_loopback:
        raise SystemExit("fake_unifi: --bind must be a loopback address")
    server = ThreadingHTTPServer((bind, port), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cert", required=True, help="PEM leaf certificate.")
    parser.add_argument("--key", required=True, help="PEM private key.")
    parser.add_argument("--bind", default="127.0.0.1", help="Loopback address only.")
    parser.add_argument("--port", type=int, default=8443)
    args = parser.parse_args(argv)
    server = build_server(args.cert, args.key, args.bind, args.port)
    host, port = server.socket.getsockname()[:2]
    print(f"fake_unifi: listening on https://{host}:{port}", flush=True)
    print(f"fake_unifi: fingerprint_sha256: {leaf_fingerprint(args.cert)}", flush=True)
    print("fake_unifi: DEV ONLY: test data from tests/fixtures/unifi_wifi/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
