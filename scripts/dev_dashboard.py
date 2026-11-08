#!/usr/bin/env python3
"""Serve the dashboard front end against fixture data, for development.

Not the real server: for real use run `housesitter serve`
(docs/DASHBOARD.md), which serves the installed web files and answers
`GET /api/dashboard` from saved snapshots. This one serves static files
from a web directory and answers `GET /api/dashboard` with a fixture
from tests/fixtures/dashboard/.

    python scripts/dev_dashboard.py                  # web/, typical fixture, port 8080
    python scripts/dev_dashboard.py --fixture large
    open http://127.0.0.1:8080/?fixture=hostile      # per-request override

Binds to 127.0.0.1 unless --bind is given. GET and HEAD only. Requests whose
Host header isn't the bind address, localhost, or an --allow-host name are
refused, which blocks DNS-rebinding attacks from other websites. When binding
to 0.0.0.0 or a LAN address, pass --allow-host for each name or IP you will
type into the browser. No directory listings and no dotfiles.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dashboard"
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# docs/DASHBOARD.md §5. The real server sends exactly these.
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'; require-trusted-types-for 'script'"
)
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def host_name(header: str) -> str:
    """The host part of a Host header, without the port."""
    header = header.strip().lower()
    if header.startswith("["):
        return header.split("]", 1)[0] + "]"
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


class Handler(http.server.SimpleHTTPRequestHandler):
    default_fixture = "typical"
    allowed_hosts = frozenset(LOCAL_HOSTS)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def _refuse(self) -> bool:
        """Refuse bad Host headers and dotfiles. Returns True if refused."""
        if host_name(self.headers.get("Host", "")) not in self.allowed_hosts:
            self.send_error(421, "unexpected Host header")
            return True
        path = unquote(urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.replace("\\", "/").split("/") if part):
            self.send_error(404)
            return True
        return False

    def do_HEAD(self):  # noqa: N802 (stdlib naming)
        if self._refuse():
            return
        if urlsplit(self.path).path == "/api/dashboard":
            self._api(body=False)
            return
        super().do_HEAD()

    def do_GET(self):  # noqa: N802 (stdlib naming)
        if self._refuse():
            return
        if urlsplit(self.path).path == "/api/dashboard":
            self._api(body=True)
            return
        super().do_GET()

    def _api(self, body: bool):
        query = parse_qs(urlsplit(self.path).query)
        name = query.get("fixture", [self.default_fixture])[0]
        path = FIXTURES / f"{name}.json"
        if not NAME_RE.fullmatch(name) or not path.is_file():
            self.send_error(404, "unknown fixture")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body:
            self.wfile.write(data)

    def _read_only(self):
        self.send_error(405, "read-only")

    def __getattr__(self, name):
        # BaseHTTPRequestHandler answers 501 for methods it has no do_X for.
        # Every method other than GET and HEAD gets 405 instead.
        if name.startswith("do_"):
            return self._read_only
        raise AttributeError(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--web", type=Path, default=ROOT / "web", help="static files directory")
    parser.add_argument("--fixture", default="typical", help="default fixture name")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-host", action="append", default=[],
                        help="extra Host header name to accept, e.g. the Pi's hostname")
    args = parser.parse_args(argv)
    if not args.web.is_dir():
        print(f"dev_dashboard: no web directory at {args.web}", file=sys.stderr)
        return 2
    Handler.default_fixture = args.fixture
    Handler.allowed_hosts = frozenset(
        LOCAL_HOSTS | {args.bind.lower()} | {h.lower() for h in args.allow_host}
    )
    handler = functools.partial(Handler, directory=str(args.web))
    with http.server.ThreadingHTTPServer((args.bind, args.port), handler) as server:
        print(f"serving {args.web} on http://{args.bind}:{args.port}/ (fixture: {args.fixture})")
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
