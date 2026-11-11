"""Smoke test for scripts/fake_unifi.py against localhost only.

Generates a throwaway certificate with openssl (skipped when openssl
is missing, or failed when HOUSESITTER_REQUIRE_TEST_TOOLS=1), boots the
fake on an OS-assigned port, and checks the startup fingerprint plus the
login, stat, and logout routes. No real network, no sudo, no system
paths changed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path

from core.helpers import require_test_tool

ROOT = Path(__file__).resolve().parent.parent
FAKE = ROOT / "scripts" / "fake_unifi.py"

openssl = shutil.which("openssl")


def wait_for_line(process, needle, timeout=20.0):
    # type: (subprocess.Popen, str, float) -> str
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        if process.poll() is not None and not buf:
            break
        chunk = process.stdout.read1(4096) if process.stdout else b""
        if not chunk:
            time.sleep(0.05)
            continue
        buf += chunk.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            if needle in line:
                return line
    raise AssertionError(f"never saw {needle!r} in fake_unifi output")


def test_fake_unifi_serves_fixtures_over_tls(tmp_path):
    require_test_tool(openssl is not None, "openssl is not installed")
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            str(FAKE),
            "--cert",
            str(cert),
            "--key",
            str(key),
            "--port",
            "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    try:
        listening = wait_for_line(proc, "listening on https://127.0.0.1:")
        port = int(listening.rsplit(":", 1)[1].rstrip())
        printed = wait_for_line(proc, "fingerprint_sha256:")
        shown = printed.split("fingerprint_sha256:")[1].strip().replace(":", "").lower()
        der = ssl.PEM_cert_to_DER_cert(cert.read_bytes().decode("ascii"))
        assert shown == hashlib.sha256(der).hexdigest()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        def request(method, path, body=None):
            conn = http.client.HTTPSConnection(
                "127.0.0.1", port, context=context, timeout=10
            )
            payload = json.dumps(body).encode() if body is not None else None
            headers = {"Content-Type": "application/json"} if payload else {}
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            data = response.read()
            headers_out = {k.lower(): v for k, v in response.getheaders()}
            conn.close()
            return response.status, headers_out, json.loads(data.decode())

        assert request("GET", "/")[0] == 200
        status, _, sysinfo = request(
            "GET", "/proxy/network/api/s/default/stat/sysinfo"
        )
        assert status == 200
        assert sysinfo["data"][0]["version"] == "9.0.114"
        status, headers, _ = request(
            "GET", "/proxy/network/api/s/default/stat/device"
        )
        assert status == 200
        status, login_headers, login = request(
            "POST", "/api/auth/login", {"username": "u", "password": "pw1"}
        )
        assert status == 200
        assert login["meta"]["rc"] == "ok"
        cookie = login_headers.get("set-cookie", "")
        assert cookie.split(";", 1)[0].split("=", 1)[0] == "TOKEN"
        assert login_headers.get("x-csrf-token", "")
        denied, _, _ = request(
            "POST", "/api/auth/login", {"username": "u", "password": ""}
        )
        assert denied == 401
        assert request("POST", "/api/auth/logout", {})[0] == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
