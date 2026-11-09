"""Pin the read-only dashboard server (docs/DASHBOARD.md §5).

Starts the REAL server (``core/serve.py`` on port 0, in a thread) over
stores seeded with real runner snapshots (``tests/dashboard_seed.py``).
All data is invented; ``tests/test_hygiene.py`` must stay green.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dashboard_seed as seed
from housesitter.core import serve as serve_mod
from housesitter.core.cli import main as cli_main
from housesitter.core.dashboard import clear_cache
from housesitter.core.registry import Registry
from housesitter.modules.unifi_wifi import UniFiWifi

START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture()
def seeded_home(tmp_path):
    seed.seed_snapshots(tmp_path, count=2, start=START)
    return tmp_path


@pytest.fixture()
def web_tree(tmp_path):
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<p>hello</p>", encoding="utf-8")
    (root / "app.js").write_text("console.log(1)", encoding="utf-8")
    (root / ".secret").write_text("nope", encoding="utf-8")
    (root / "DESIGN.md").write_text("design notes", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "page.html").write_text("nested", encoding="utf-8")
    return root


@pytest.fixture()
def live(seeded_home, web_tree):
    server, thread, url = seed.start_server(
        seeded_home, web_directory=web_tree
    )
    yield url
    seed.stop_server(server)


def fetch(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def raw_request(url, method, host_value=None, skip_host=False):
    """One raw HTTP/1.1 request, with full control over the Host header."""
    parsed = urllib.request.urlparse(url)
    conn = http.client.HTTPConnection(
        parsed.hostname, parsed.port, timeout=5
    )
    try:
        # skip_host=True always: http.client would otherwise add its own
        # Host header beside ours, which the server rightly refuses as a
        # duplicate.
        conn.putrequest(method, parsed.path or "/", skip_host=True)
        if not skip_host:
            conn.putheader("Host", host_value or parsed.hostname)
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def duplicate_host_request(url):
    parsed = urllib.request.urlparse(url)
    sock = __import__("socket").create_connection(
        (parsed.hostname, parsed.port), timeout=5
    )
    try:
        sock.sendall(
            f"GET {parsed.path or '/'} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}\r\n"
            "Host: evil.example\r\n"
            "Connection: close\r\n\r\n".encode("latin-1")
        )
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        head = data.split(b"\r\n", 1)[0]
        return int(head.split(b" ")[1])
    finally:
        sock.close()


# -- static files ------------------------------------------------------------


class TestStaticFiles:
    def test_root_serves_index(self, live):
        status, headers, body = fetch(live + "/")
        assert status == 200
        assert b"hello" in body
        assert headers["Content-Type"] == "text/html; charset=utf-8"

    def test_js_content_type(self, live):
        status, headers, body = fetch(live + "/app.js")
        assert status == 200
        assert headers["Content-Type"] == "text/javascript; charset=utf-8"

    @pytest.mark.parametrize("path", [
        "/nope.html", "/sub/", "/sub/page.html", "/.secret",
        "/%2esecret", "/DESIGN.md", "/..%2fsecret", "/%2e%2e/",
        "/web/../pyproject.toml", "/index.html/../app.js",
    ])
    def test_unknown_hidden_and_traversal_are_404(self, live, path):
        status, _, _ = fetch(live + path)
        assert status == 404

    def test_backslash_is_404(self, live):
        assert raw_request(live + "/app.js", "GET")[0] == 200
        assert raw_request(live + "/..\\app.js", "GET")[0] == 404


# -- methods and HEAD ----------------------------------------------------------


class TestMethods:
    @pytest.mark.parametrize("method", [
        "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND",
    ])
    def test_only_get_and_head(self, live, method):
        assert fetch(live + "/api/dashboard", method=method)[0] == 405
        assert fetch(live + "/", method=method)[0] == 405

    def test_head_matches_get_headers_with_no_body(self, live):
        get_status, get_headers, get_body = fetch(live + "/api/dashboard")
        head_status, head_headers, head_body = fetch(
            live + "/api/dashboard", method="HEAD"
        )
        assert get_status == head_status == 200
        assert head_body == b""
        assert head_headers["Content-Length"] == get_headers["Content-Length"]
        assert int(head_headers["Content-Length"]) == len(get_body) > 0

    def test_head_static(self, live):
        get_status, get_headers, get_body = fetch(live + "/")
        head_status, head_headers, head_body = fetch(live + "/", method="HEAD")
        assert get_status == head_status == 200
        assert head_body == b""
        assert head_headers["Content-Length"] == get_headers["Content-Length"]


# -- Host check -----------------------------------------------------------------


class TestHostCheck:
    @pytest.mark.parametrize("host", [
        "evil.example", "evil.example:8080", "127.0.0.1.evil.example",
    ])
    def test_foreign_host_is_421(self, live, host):
        assert raw_request(live + "/api/dashboard", "GET", host)[0] == 421
        assert raw_request(live + "/", "GET", host)[0] == 421

    def test_missing_and_duplicate_host_are_421(self, live):
        assert raw_request(live + "/", "GET", skip_host=True)[0] == 421
        assert duplicate_host_request(live + "/") == 421

    def test_case_insensitive_host_passes(self, live):
        # 127.0.0.1 reaches this server; an uppercase Host must pass too.
        parsed = urllib.request.urlparse(live)
        conn = http.client.HTTPConnection(
            parsed.hostname, parsed.port, timeout=5
        )
        try:
            conn.putrequest("GET", "/", skip_host=True)
            conn.putheader("Host", "LOCALHOST")
            conn.endheaders()
            status = conn.getresponse().status
        finally:
            conn.close()
        assert status == 200


# -- API ------------------------------------------------------------------------


class TestApi:
    def test_dashboard_answers_saved_data(self, live):
        from test_dashboard_contract import check_view

        status, headers, body = fetch(live + "/api/dashboard")
        assert status == 200
        assert headers["Content-Type"] == "application/json; charset=utf-8"
        # Real controller counts can disagree with the client list
        # (DASHBOARD.md §4.4), so the realism cross-check is off here.
        check_view(json.loads(body), realistic=False)

    def test_query_string_ignored_for_routing(self, live):
        from test_dashboard_contract import check_view

        status, _, body = fetch(live + "/api/dashboard?fixture=typical")
        assert status == 200
        check_view(json.loads(body), realistic=False)

    def test_unavailable_store_is_503_json(self, tmp_path, web_tree):
        seed.write_test_config(tmp_path, server_port=1)
        root = tmp_path / ".local" / "share" / "housesitter"
        root.mkdir(parents=True)
        target = tmp_path / "elsewhere"
        target.mkdir()
        root.rename(tmp_path / ".local" / "share" / "housesitter-real")
        os.symlink(target, root)
        try:
            server, _, url = seed.start_server(
                tmp_path, web_directory=web_tree
            )
            try:
                status, headers, body = fetch(url + "/api/dashboard")
            finally:
                seed.stop_server(server)
        finally:
            os.unlink(root)
        assert status == 503
        assert json.loads(body) == {"error": "dashboard data unavailable"}
        assert headers["Cache-Control"] == "no-store"

    def test_missing_data_dir_serves_never_without_creating(self, tmp_path):
        seed.write_test_config(tmp_path, server_port=1)
        server, _, url = seed.start_server(tmp_path)
        try:
            status, _, body = fetch(url + "/api/dashboard")
        finally:
            seed.stop_server(server)
        assert status == 200
        assert json.loads(body)["unifi_wifi"] is None
        assert not (tmp_path / ".local").exists()


# -- headers on every response ----------------------------------------------------


class TestSecurityHeaders:
    @pytest.mark.parametrize("path", ["/", "/nope.html", "/api/dashboard"])
    def test_headers_present_on_success_and_404(self, live, path):
        _, headers, _ = fetch(live + path)
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_headers_present_on_405_and_421(self, live):
        _, headers_405, _ = fetch(live + "/", method="POST")
        assert headers_405["Cache-Control"] == "no-store"
        assert "Content-Security-Policy" in headers_405
        _, headers_421, _ = raw_request(live + "/", "GET", "evil.example")
        lowered = {key.lower(): value for key, value in headers_421.items()}
        assert lowered.get("cache-control") == "no-store"
        assert "content-security-policy" in lowered

    def test_csp_matches_dev_server(self):
        from test_dashboard_contract import dev_dashboard

        assert serve_mod.CSP == dev_dashboard.CSP

    def test_error_bodies_echo_nothing(self, live):
        marker = "MARKER-never-echo-12345"
        for path in (f"/{marker}", f"/api/dashboard/{marker}"):
            status, _, body = fetch(live + path)
            assert status == 404
            assert marker.encode() not in body
        status, _, body = fetch(live + "/", method="POST")
        assert status == 405
        assert body == b"method not allowed"


# -- read-only guarantee ------------------------------------------------------------


class TestReadOnly:
    def test_request_sweep_writes_nothing(self, tmp_path):
        home = tmp_path
        seed.seed_snapshots(home, count=2, start=START)

        def snapshot_tree() -> dict:
            found = {}
            for path in sorted(home.rglob("*")):
                st = os.lstat(path)
                digest = None
                if stat.S_ISREG(st.st_mode):
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                found[str(path.relative_to(home))] = (
                    stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                    st.st_size, digest,
                )
            return found

        before = snapshot_tree()
        server, _, url = seed.start_server(home)
        try:
            for path in ["/", "/app.js", "/api/dashboard",
                         "/api/dashboard?fixture=x", "/nope"]:
                fetch(url + path)
            fetch(url + "/api/dashboard", method="HEAD")
            fetch(url + "/", method="POST")
            raw_request(url + "/", "GET", "evil.example")
        finally:
            seed.stop_server(server)
        assert snapshot_tree() == before

    def test_source_imports_nothing_that_touches_network(self):
        for name in ("serve", "dashboard"):
            path = ROOT / "src" / "housesitter" / "core" / f"{name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported: set = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imported.add(node.module)
            forbidden = {
                "housesitter.core.http", "urllib.request", "http.client",
                "subprocess",
            }
            assert not (imported & forbidden), (name, imported & forbidden)


# -- startup -----------------------------------------------------------------------


class TestStartup:
    def test_port_in_use_is_exit_2_with_one_line(self, tmp_path):
        import socket as socket_mod

        seed.write_test_config(tmp_path, server_port=1)
        blocker = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        blocker.setsockopt(
            socket_mod.SOL_SOCKET, socket_mod.SO_REUSEADDR, 1
        )
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        seed.write_test_config(
            tmp_path, server_port=port,
            dashboard={"bind": "127.0.0.1", "port": port},
        )
        out, err = io.StringIO(), io.StringIO()
        try:
            code = cli_main(
                ["serve"], env={"HOME": str(tmp_path)},
                stdout=out, stderr=err,
                registry=Registry.from_classes([UniFiWifi]),
            )
        finally:
            blocker.close()
        assert code == 2
        lines = err.getvalue().splitlines()
        assert len(lines) == 1
        assert f"127.0.0.1:{port}" in lines[0]
        assert "address already in use" in lines[0]

    def test_unavailable_address_is_exit_2(self, tmp_path):
        seed.write_test_config(
            tmp_path, server_port=1,
            dashboard={"bind": "192.0.2.1", "port": 18080},
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(
            ["serve"], env={"HOME": str(tmp_path)},
            stdout=out, stderr=err,
            registry=Registry.from_classes([UniFiWifi]),
        )
        assert code == 2
        lines = err.getvalue().splitlines()
        assert len(lines) == 1
        assert "192.0.2.1:18080" in lines[0]

    def test_invalid_config_is_exit_2_with_config_line(self, tmp_path):
        seed.write_test_config(
            tmp_path, server_port=1, dashboard={"port": 0}
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(
            ["serve"], env={"HOME": str(tmp_path)},
            stdout=out, stderr=err,
            registry=Registry.from_classes([UniFiWifi]),
        )
        assert code == 2
        lines = err.getvalue().splitlines()
        assert len(lines) == 1
        assert lines[0] == (
            "housesitter: housesitter: config_invalid: "
            "housesitter.dashboard.port: port must be an integer 1..65535"
        )

    def test_start_lines_and_clean_exit(self, tmp_path, monkeypatch):
        seed.write_test_config(
            tmp_path, server_port=1,
            dashboard={"bind": "127.0.0.1", "port": 8080},
        )
        calls: list = []
        monkeypatch.setattr(
            serve_mod.DashboardServer, "serve_forever",
            lambda self, **kwargs: calls.append(kwargs),
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(
            ["serve"], env={"HOME": str(tmp_path)},
            stdout=out, stderr=err,
            registry=Registry.from_classes([UniFiWifi]),
        )
        assert code == 0
        assert out.getvalue().splitlines() == [
            "serving the dashboard on http://127.0.0.1:8080/ (read-only)"
        ]
        assert calls

    def test_lan_bind_warns(self, tmp_path, monkeypatch):
        seed.write_test_config(
            tmp_path, server_port=1,
            dashboard={"bind": "0.0.0.0", "port": 8080},
        )
        monkeypatch.setattr(
            serve_mod.DashboardServer, "serve_forever", lambda self, **kw: None
        )
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(
            ["serve"], env={"HOME": str(tmp_path)},
            stdout=out, stderr=err,
            registry=Registry.from_classes([UniFiWifi]),
        )
        assert code == 0
        lines = out.getvalue().splitlines()
        assert lines[0] == (
            "serving the dashboard on http://0.0.0.0:8080/ (read-only)"
        )
        assert "MACs" in lines[1]


# -- packaging -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the wheel once and install it into a fresh venv once.

    Skips only when the build or venv tooling is unavailable; checks on
    what the wheel contains or how it runs belong in the tests.
    """
    base = tmp_path_factory.mktemp("wheel")
    try:
        proc = subprocess.run(
            # Offline: build with the dev venv's own setuptools and
            # wheel (pinned in the dev extra), never fetching from PyPI.
            [sys.executable, "-m", "pip", "wheel", ".", "--no-deps",
             "--no-build-isolation", "--no-index", "-w", str(base)],
            cwd=str(ROOT),
            capture_output=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("wheel build tooling unavailable")
    if proc.returncode != 0:
        pytest.skip(
            "wheel build tooling unavailable "
            f"(rc={proc.returncode}): "
            f"{proc.stderr.decode()[-300:]}"
        )
    wheels = list(base.glob("*.whl"))
    assert len(wheels) == 1
    venv = base / "fresh"
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            check=True, capture_output=True, timeout=120,
        )
        subprocess.run(
            [str(venv / "bin" / "pip"), "install", "--no-index",
             str(wheels[0])],
            check=True, capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError) as exc:
        pytest.skip(f"fresh-venv install unavailable: {exc}")
    return wheels[0], venv


class TestPackaging:
    EXPECTED_WEB = ("index.html", "eink.html", "dashboard.js", "format.js",
                    "dashboard.css", "eink.css")

    def test_wheel_ships_web_files(self, built_wheel):
        import zipfile

        wheel, venv = built_wheel
        names = set(zipfile.ZipFile(wheel).namelist())
        for leaf in self.EXPECTED_WEB:
            assert f"housesitter/web/{leaf}" in names
        shipped_web = {n for n in names if n.startswith("housesitter/web/")}
        assert shipped_web == {f"housesitter/web/{leaf}" for leaf in self.EXPECTED_WEB}
        # A wheel install in a fresh venv finds the files at runtime.
        probe = subprocess.run(
            [str(venv / "bin" / "python"), "-c",
             "from housesitter.core.serve import web_dir,"
             " load_static_files;"
             " print(sorted(load_static_files(web_dir())))"],
            capture_output=True, timeout=60,
        )
        assert probe.returncode == 0, probe.stderr.decode()[-500:]
        found = probe.stdout.decode()
        for leaf in self.EXPECTED_WEB:
            assert f"'/{leaf}'" in found
        # The editable install under test finds them too.
        table = serve_mod.load_static_files(serve_mod.web_dir())
        assert sorted(table) == sorted(f"/{leaf}" for leaf in self.EXPECTED_WEB)

    def test_wheel_ships_module_skill_md(self, built_wheel, tmp_path):
        import zipfile

        wheel, venv = built_wheel
        names = set(zipfile.ZipFile(wheel).namelist())
        # Every module directory that carries a SKILL.md must ship it:
        # otherwise `housesitter describe` warns "SKILL.md ... is missing"
        # and Muse never gets the module guide.
        expected = {
            f"housesitter/modules/{d.name}/SKILL.md"
            for d in (ROOT / "src" / "housesitter" / "modules").iterdir()
            if (d / "SKILL.md").is_file()
        }
        assert expected, "no module SKILL.md found in the source tree"
        assert expected <= names, f"missing from the wheel: {sorted(expected - names)}"
        # A wheel install in a fresh venv describes the skill, not a
        # "missing" warning. A failing describe fails the test.
        home = tmp_path / "fresh-home"
        home.mkdir()
        probe = subprocess.run(
            [str(venv / "bin" / "housesitter"), "describe"],
            capture_output=True, timeout=60,
            env={"HOME": str(home), "PATH": os.environ["PATH"]},
        )
        assert probe.returncode == 0, probe.stderr.decode()[-500:]
        envelope = json.loads(probe.stdout.decode())
        warnings = envelope.get("warnings", [])
        assert not any("SKILL.md" in w for w in warnings), warnings
        modules = {m["id"]: m for m in envelope["data"]["modules"]}
        skill = modules["unifi_wifi"]["skill"]
        assert skill, "installed describe has no unifi_wifi skill text"
        source = (
            ROOT / "src" / "housesitter" / "modules"
            / "unifi_wifi" / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert source[:200] in skill


# -- review findings (adversarial review of #14) -----------------------------


def _raw_bytes(url: str, payload: bytes) -> bytes:
    import socket as _socket
    from urllib.parse import urlsplit as _urlsplit

    parts = _urlsplit(url)
    with _socket.create_connection((parts.hostname, parts.port), timeout=5) as conn:
        conn.sendall(payload)
        chunks = []
        while True:
            try:
                block = conn.recv(65536)
            except _socket.timeout:
                break
            if not block:
                break
            chunks.append(block)
    return b"".join(chunks)


@pytest.mark.parametrize(
    "payload", [b"GET GIBBERISH\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"GET /\r\n\r\n"]
)
def test_malformed_requests_still_carry_security_headers(live, payload):
    reply = _raw_bytes(live, payload)
    head = reply.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    assert head.startswith("HTTP/1."), reply[:80]
    assert "Cache-Control: no-store" in head
    assert "X-Content-Type-Options: nosniff" in head
    assert "Content-Security-Policy: " + serve_mod.CSP in head
    assert "Content-Type: text/plain" in head


@pytest.mark.parametrize(
    ("header", "bind", "allowed", "ok"),
    [
        ("[2001:db8::5]:8080", "0.0.0.0", {"2001:db8::5"}, True),
        ("[2001:db8::5]", "::", {"[2001:db8::5]"}, True),
        ("[::]:8080", "::", set(), True),
        ("[2001:db8::6]:8080", "::", {"2001:db8::5"}, False),
        ("2001:db8::5", "::", {"2001:db8::5"}, True),
    ],
)
def test_ipv6_hosts_compare_without_brackets(header, bind, allowed, ok):
    assert serve_mod.host_allowed([header], bind, frozenset(allowed)) is ok


def test_request_log_hides_userinfo_and_is_capped(live, capfd):
    capfd.readouterr()
    _raw_bytes(
        live,
        b"GET http://user:PLANTED-SECRET-log@127.0.0.1/?"
        + b"q" * 5000
        + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n",
    )
    err = capfd.readouterr().err
    assert "PLANTED-SECRET-log" not in err
    assert err.strip(), "the request is still logged"
    assert all(len(line) <= 500 for line in err.splitlines())


def test_every_source_package_is_listed_for_packaging():
    """pyproject lists packages explicitly (web/ is mapped in): a new
    package under src/ must be added there, or it would silently not ship."""
    import re as _re

    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = _re.search(r"packages = \[(.*?)\]", text, _re.S).group(1)
    listed = set(_re.findall(r'"([a-z_.]+)"', block))
    src = ROOT / "src"
    found = {
        ".".join(init.parent.relative_to(src).parts)
        for init in src.rglob("__init__.py")
    }
    assert found <= listed, sorted(found - listed)
    assert "housesitter.web" in listed
