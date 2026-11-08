"""Seed temporary HOME stores for dashboard tests (not collected).

Drives ``housesitter.unifi_wifi.snapshot`` through the real runner
against a loopback fixture server, exactly like the module tests do, so
every saved file is what the real code writes. All data is invented:
upstream payloads come from ``tests/fixtures/unifi_wifi/`` and every
secret value contains ``PLANTED-SECRET``.
"""

from __future__ import annotations

import io
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from core.helpers import Canned, ScriptedServer, canned_handler
from housesitter.core import runner
from housesitter.core.config import load_config
from housesitter.core.registry import Registry
from housesitter.modules.unifi_wifi import UniFiWifi

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "unifi_wifi"
REPO_WEB = Path(__file__).resolve().parent.parent / "web"

LOGIN = "/api/auth/login"
LOGOUT = "/api/auth/logout"
PREFIX = "/proxy/network/api/s/default"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def typical_routes() -> dict:
    """Full success route table for the invented house (UniFi OS)."""
    return {
        ("POST", LOGIN): Canned(
            status=200, body=fixture_bytes("unifi_os_login.json")
        ),
        ("GET", PREFIX + "/stat/sysinfo"): Canned(
            status=200, body=fixture_bytes("unifi_os_sysinfo.json")
        ),
        ("GET", PREFIX + "/stat/device"): Canned(
            status=200, body=fixture_bytes("unifi_os_device.json")
        ),
        ("GET", PREFIX + "/stat/sta"): Canned(
            status=200, body=fixture_bytes("unifi_os_sta.json")
        ),
        ("POST", LOGOUT): Canned(status=200, body=b"{}"),
    }


def hostile_routes() -> dict:
    """Same, but devices and clients carry injection-style names."""
    routes = typical_routes()
    routes[("GET", PREFIX + "/stat/device")] = Canned(
        status=200, body=fixture_bytes("hostile_device.json")
    )
    routes[("GET", PREFIX + "/stat/sta")] = Canned(
        status=200, body=fixture_bytes("hostile_sta.json")
    )
    return routes


def write_test_config(
    home: Path,
    *,
    server_port: int | None = None,
    enabled: bool = True,
    dashboard: dict | None = None,
    identifiers: str = "full",
) -> None:
    """Write a config.json plus a planted secret into a fake HOME."""
    module: dict = {"enabled": enabled}
    if enabled:
        module.update(
            {
                # A placeholder port for config-only tests; seeding passes
                # the fixture server's real port.
                "base_url": f"https://192.0.2.1:{server_port or 8443}",
                "site": "default",
                "flavor": "unifi_os",
                "auth": {
                    "username": "housesitter-readonly",
                    "password_secret": "unifi_password",
                },
                "tls": {},
                "request_timeout_s": 15,
            }
        )
    top: dict = {
        "housesitter": {"identifiers": identifiers},
        "modules": {"unifi_wifi": module},
    }
    if dashboard is not None:
        top["housesitter"]["dashboard"] = dashboard
    config_dir = home / ".config" / "housesitter"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        json.dumps(top), encoding="utf-8"
    )
    secrets_dir = config_dir / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secrets_dir / "unifi_password"
    secret_path.write_text(
        "PLANTED-SECRET-seed-password", encoding="utf-8"
    )
    os.chmod(secret_path, 0o600)


def seed_snapshots(
    home: Path,
    *,
    count: int,
    start: datetime,
    step_s: int = 60,
    routes_kind: str = "typical",
    labels: list | None = None,
    identifiers: str = "full",
    dashboard: dict | None = None,
) -> list[str]:
    """Run ``snapshot`` ``count`` times; return the saved ids, oldest first.

    ``start`` is the first snapshot's collected-at (each later snapshot
    advances by ``step_s``). All runs share one fixture server.
    """
    from housesitter.core import http as http_core

    routes = hostile_routes() if routes_kind == "hostile" else typical_routes()
    server = ScriptedServer(canned_handler(routes))
    try:
        write_test_config(
            home,
            server_port=server.port,
            identifiers=identifiers,
            dashboard=dashboard,
        )

        def factory(scheme: str, host: str, port: int, **kwargs: object) -> object:
            return http_core.Client(
                "http", "127.0.0.1", server.port,
                allow=kwargs.get("allow"),
                default_timeout_s=kwargs.get("default_timeout_s"),
            )

        registry = Registry.from_classes([UniFiWifi])
        env = {"HOME": str(home)}
        ids: list[str] = []
        for index in range(count):
            moment = start + timedelta(seconds=index * step_s)
            params: dict = {}
            if labels is not None and index < len(labels):
                params = {"label": labels[index]}
            outcome = runner.run_command(
                "housesitter.unifi_wifi.snapshot",
                params,
                registry=registry,
                env=env,
                now=lambda moment=moment: moment,
                client_factory=factory,
                log_stream=io.StringIO(),
            )
            assert outcome.exit_code == 0, runner.format_error_line(outcome)
            assert outcome.envelope.get("saved")
            ids.append(outcome.envelope["saved"])
        return ids
    finally:
        server.close()


def module_dir(home: Path) -> Path:
    return home / ".local" / "share" / "housesitter" / "unifi_wifi"


def load_view_config(home: Path) -> tuple:
    """The (registry, config) pair the server builds views with."""
    registry = Registry.from_classes([UniFiWifi])
    config = load_config(registry, env={"HOME": str(home)})
    return registry, config


def start_server(
    home: Path,
    *,
    bind: str = "127.0.0.1",
    port: int = 0,
    web_directory: Path | None = None,
) -> tuple:
    """Start the real dashboard server over a HOME; return (server, thread, url)."""
    from housesitter.core import serve as serve_mod

    registry = Registry.from_classes([UniFiWifi])
    env = {"HOME": str(home)}

    def config_loader() -> object:
        return load_config(registry, env=env)

    server = serve_mod.create_server(
        bind,
        port,
        registry=registry,
        config_loader=config_loader,
        env=env,
        web_directory=(
            web_directory if web_directory is not None else REPO_WEB
        ),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    thread.start()
    host = f"[{bind}]" if ":" in bind else bind
    return server, thread, f"http://{host}:{server.server_address[1]}"


def stop_server(server: object) -> None:
    server.shutdown()  # type: ignore[attr-defined]
    server.server_close()  # type: ignore[attr-defined]
