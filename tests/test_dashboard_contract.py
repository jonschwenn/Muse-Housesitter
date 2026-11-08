"""The dashboard view-model contract (docs/DASHBOARD.md).

Pins the exact keys of every object in housesitter.dashboard/1, checks the
committed fixtures against it and against their generator, and smoke-tests
the development server.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import dashboard_seed as seed
from housesitter.core.dashboard import clear_cache

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dashboard"
START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


make_fixtures = load_script("make_dashboard_fixtures")
dev_dashboard = load_script("dev_dashboard")

# Exact key sets, one per object type. Must match the tables in docs/DASHBOARD.md.
KEYS = {
    "view": {"schema", "generated_at", "housesitter_version", "house_label", "modules",
             "muse_note", "unifi_wifi"},
    "module": {"id", "title", "version", "status", "last_success_at", "last_attempt_at",
               "last_error"},
    "error": {"code", "message"},
    "note": {"text", "written_at"},
    "unifi_wifi": {"snapshots", "latest", "previous"},
    "snapshots": {"total", "recent"},
    "history": {"id", "collected_at", "label", "ap_count", "client_count", "clients_by_band"},
    "snapshot": {"id", "collected_at", "label", "source", "warnings", "summary", "aps",
                 "clients"},
    "source": {"kind", "flavor", "controller_version", "site"},
    "summary": {"ap_count", "ap_online_count", "client_count", "clients_by_band",
                "clients_by_ap", "clients_by_ssid"},
    "band_count": {"band", "count"},
    "ap_count": {"ap_id", "ap_name", "count"},
    "ssid_count": {"ssid", "count"},
    "ap": {"id", "name", "model", "firmware", "online", "uptime_s", "client_count",
           "satisfaction_pct", "uplink_type", "uplink_speed_mbps", "radios"},
    "radio": {"band", "channel", "channel_setting", "width_mhz", "tx_power_dbm",
              "tx_power_mode", "channel_utilization_pct", "self_rx_pct", "self_tx_pct",
              "client_count", "satisfaction_pct", "tx_retry_pct_cumulative"},
    "client": {"id", "ip", "name", "vendor", "private_mac", "ssid", "ap_id", "ap_name",
               "band", "channel", "wifi_standard", "wifi_standard_raw", "signal_dbm",
               "noise_dbm", "tx_rate_mbps", "rx_rate_mbps", "satisfaction_pct",
               "tx_retry_pct_cumulative", "tx_retry_pct_recent", "connected_s", "idle_s",
               "guest"},
}
STATUSES = {"ok", "error", "never"}
BANDS = {"2.4GHz", "5GHz", "6GHz", "unknown"}


def keys(obj, kind):
    assert isinstance(obj, dict), kind
    assert set(obj) == KEYS[kind], (kind, set(obj) ^ KEYS[kind])


def check_snapshot(snap, realistic=True):
    keys(snap, "snapshot")
    keys(snap["source"], "source")
    keys(snap["summary"], "summary")
    for entry in snap["summary"]["clients_by_band"]:
        keys(entry, "band_count")
        assert entry["band"] in BANDS
    for entry in snap["summary"]["clients_by_ap"]:
        keys(entry, "ap_count")
    for entry in snap["summary"]["clients_by_ssid"]:
        keys(entry, "ssid_count")
    for ap in snap["aps"]:
        keys(ap, "ap")
        for radio in ap["radios"]:
            keys(radio, "radio")
            assert radio["band"] in BANDS
    aps = {ap["id"]: ap for ap in snap["aps"]}
    for client in snap["clients"]:
        keys(client, "client")
        assert client["band"] in BANDS
        assert client["ap_id"] is None or client["ap_id"] in aps
        # Unplaced clients have no AP name, except when identifiers are
        # hidden (D24): then ap_id is null for every client, and ap_name
        # still names the AP.
        if client["ap_id"] is None and not ids_hidden(snap):
            assert client["ap_name"] is None
    ids = [c["id"] for c in snap["clients"]]
    if ids_hidden(snap):
        # identifiers: "none" (D24): every identifier is null, so ids can't
        # be unique and per-AP counts can't be matched by ap_id.
        assert all(c["ip"] is None and c["ap_id"] is None for c in snap["clients"])
        assert all(e["ap_id"] is None for e in snap["summary"]["clients_by_ap"])
        return
    assert len(ids) == len(set(ids)), "client ids must be unique within a snapshot"
    check_summary(snap, aps)
    if realistic:
        check_controller_counts(snap, aps)


def ids_hidden(snap):
    """True when the view was built with identifiers: "none" (D24)."""
    return all(c["id"] is None for c in snap["clients"]) and all(
        a["id"] is None for a in snap["aps"]
    ) and bool(snap["aps"] or snap["clients"])


def check_summary(snap, aps):
    """Contract: summary counts are derived from the lists, so they always agree."""
    clients = snap["clients"]
    summary = snap["summary"]
    assert summary["client_count"] == len(clients)
    assert summary["ap_count"] == len(aps)
    assert summary["ap_online_count"] == sum(1 for a in aps.values() if a["online"])
    by_band = {e["band"]: e["count"] for e in summary["clients_by_band"]}
    assert set(by_band) == BANDS
    for band in BANDS:
        assert by_band[band] == sum(1 for c in clients if c["band"] == band)
    for entry in summary["clients_by_ap"]:
        assert entry["count"] == sum(1 for c in clients if c["ap_id"] == entry["ap_id"])
    placed = sum(e["count"] for e in summary["clients_by_ap"])
    assert placed == sum(1 for c in clients if c["ap_id"] is not None)


def check_controller_counts(snap, aps):
    """Fixture realism, not a contract guarantee: AP and radio client_count come
    from the controller and can disagree with clients on real data (the edge
    fixture shows that). The other fixtures keep them consistent."""
    clients = snap["clients"]
    for ap_id, ap in aps.items():
        mine = [c for c in clients if c["ap_id"] == ap_id]
        assert ap["client_count"] == (len(mine) if ap["online"] else None)
        if not ap["online"]:
            assert not mine
        for radio in ap["radios"]:
            on_radio = [c for c in mine if c["band"] == radio["band"]]
            if radio["client_count"] is not None:  # null: stats unavailable (offline AP)
                assert radio["client_count"] == len(on_radio), (ap["name"], radio["band"])
            for c in on_radio:
                assert c["channel"] == radio["channel"], (c["name"], radio["band"])
        for c in mine:
            assert c["ap_name"] == ap["name"]
            if c["band"] != "unknown":
                assert any(r["band"] == c["band"] for r in ap["radios"]), c["name"]


def check_view(view, realistic=True):
    keys(view, "view")
    assert view["schema"] == "housesitter.dashboard/1"
    for module in view["modules"]:
        keys(module, "module")
        assert module["status"] in STATUSES
        if module["last_error"] is not None:
            keys(module["last_error"], "error")
    if view["muse_note"] is not None:
        keys(view["muse_note"], "note")
    unifi = view["unifi_wifi"]
    if unifi is None:
        return
    keys(unifi, "unifi_wifi")
    keys(unifi["snapshots"], "snapshots")
    recent = unifi["snapshots"]["recent"]
    for entry in recent:
        keys(entry, "history")
    times = [e["collected_at"] for e in recent]
    assert times == sorted(times, reverse=True) and len(set(times)) == len(times), "newest first"
    assert len(recent) <= 50 and unifi["snapshots"]["total"] >= len(recent)
    assert unifi["latest"] is not None, "unifi_wifi is null when there is no snapshot"
    assert recent[0]["id"] == unifi["latest"]["id"]
    # previous is the second-newest snapshot, and exists exactly when there are two.
    if unifi["snapshots"]["total"] >= 2:
        assert unifi["previous"] is not None and recent[1]["id"] == unifi["previous"]["id"]
    else:
        assert unifi["previous"] is None
    for which in ("latest", "previous"):
        if unifi[which] is not None:
            check_snapshot(unifi[which], realistic=realistic)


@pytest.mark.parametrize("name", sorted(make_fixtures.FIXTURES))
def test_fixture_matches_the_contract(name):
    view = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    check_view(view, realistic=(name != "edge"))


def test_fixtures_are_up_to_date_with_their_generator():
    for name, text in make_fixtures.render_all().items():
        committed = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
        assert committed == text, f"run scripts/make_dashboard_fixtures.py ({name} is stale)"


def test_typical_fixture_tells_the_reference_story():
    view = json.loads((FIXTURES / "typical.json").read_text(encoding="utf-8"))
    by_name = {
        which: {c["name"]: c for c in view["unifi_wifi"][which]["clients"]}
        for which in ("previous", "latest")
    }
    for speaker in ("Kitchen Speaker", "Living Room Speaker", "Bedroom Speaker"):
        assert by_name["previous"][speaker]["band"] == "2.4GHz"
        assert by_name["latest"][speaker]["band"] == "5GHz"
        assert by_name["latest"][speaker]["ssid"] == "iot-example"
    assert by_name["latest"]["Patio Speaker"]["signal_dbm"] <= -75
    assert by_name["previous"]["Patio Speaker"]["signal_dbm"] <= -75
    assert by_name["latest"]["Den Speaker"]["band"] == "2.4GHz"


@pytest.fixture
def server(tmp_path):
    (tmp_path / "index.html").write_text("<p>hello</p>")
    (tmp_path / ".secret").write_text("nope")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".hidden").write_text("nope")
    handler = partial(dev_dashboard.Handler, directory=str(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def live_web(tmp_path_factory):
    """A static tree for the real server: an index, a dotfile, a
    subdir, and a DESIGN.md that must never serve."""
    root = tmp_path_factory.mktemp("live-web")
    (root / "index.html").write_text("<p>hello</p>", encoding="utf-8")
    (root / ".secret").write_text("nope", encoding="utf-8")
    (root / "DESIGN.md").write_text("design notes", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / ".hidden").write_text("nope", encoding="utf-8")
    return root


@pytest.fixture
def live(tmp_path_factory, live_web):
    """The REAL dashboard server over two saved snapshots."""
    home = tmp_path_factory.mktemp("live-home")
    seed.seed_snapshots(home, count=2, start=START)
    server, _thread, url = seed.start_server(
        home, web_directory=live_web
    )
    yield url
    seed.stop_server(server)


@pytest.fixture(params=["dev", "live"])
def either(request):
    """Each security probe runs against the dev server and the real one.

    Resolved lazily so a case only starts the server it probes.
    """
    return request.getfixturevalue(
        "server" if request.param == "dev" else "live"
    )


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    clear_cache()
    yield
    clear_cache()


def get(url, method="GET"):
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def test_every_documented_state_has_a_fixture():
    states = {
        name: json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for name in make_fixtures.FIXTURES
    }
    assert states["empty"]["modules"][0]["status"] == "never"
    assert states["empty"]["unifi_wifi"] is None
    assert states["error"]["modules"][0]["status"] == "error"
    assert states["error"]["unifi_wifi"]["latest"] is not None
    assert states["failed"]["modules"][0]["status"] == "error"
    assert states["failed"]["unifi_wifi"] is None
    assert states["hostile"]["unifi_wifi"]["previous"] is not None
    assert len(states["large"]["unifi_wifi"]["latest"]["clients"]) == 600
    assert states["typical"]["unifi_wifi"]["snapshots"]["total"] > 50
    edge = states["edge"]["unifi_wifi"]
    for snap in (edge["latest"], edge["previous"]):
        assert any(c["band"] == "unknown" for c in snap["clients"])
        assert any(c["signal_dbm"] is None for c in snap["clients"])
        assert any(c["ap_id"] is None for c in snap["clients"])
        assert any(a["name"] is None for a in snap["aps"])
        assert any(r["client_count"] is None for a in snap["aps"] for r in a["radios"])
    with pytest.raises(AssertionError):
        check_controller_counts(edge["latest"], {a["id"]: a for a in edge["latest"]["aps"]})
    offline = [a for a in states["large"]["unifi_wifi"]["latest"]["aps"] if not a["online"]]
    assert offline and all(
        r["channel_utilization_pct"] is None for a in offline for r in a["radios"]
    )


def test_dev_server_serves_fixtures_and_static_files(server):
    status, headers, body = get(server + "/api/dashboard?fixture=empty")
    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    check_view(json.loads(body))
    status, headers, body = get(server + "/")
    assert status == 200 and b"hello" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]


@pytest.mark.parametrize("fixture", ["nope", "../typical", "TYPICAL", "%2e%2e%2ftypical"])
def test_dev_server_rejects_unknown_fixtures(server, fixture):
    assert get(server + "/api/dashboard?fixture=" + fixture)[0] == 404


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "PROPFIND"]
)
def test_servers_are_read_only(either, method):
    assert get(either + "/api/dashboard", method=method)[0] == 405


def test_servers_answer_head_for_the_api(either):
    status, headers, body = get(either + "/api/dashboard", method="HEAD")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


def test_servers_send_the_documented_csp(either):
    csp = get(either + "/")[1]["Content-Security-Policy"]
    assert csp == dev_dashboard.CSP, "the real server must send the same CSP"
    for directive in ("script-src 'self'", "form-action 'none'", "frame-ancestors 'none'",
                      "require-trusted-types-for 'script'"):
        assert directive in csp
    doc = (ROOT / "docs" / "DASHBOARD.md").read_text(encoding="utf-8")
    assert " ".join(doc.split()).count(csp) == 1, "DASHBOARD.md §5 must quote the same CSP"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "127.0.0.1.evil.example"])
def test_servers_refuse_foreign_host_headers(either, host):
    import http.client

    hostport = either.removeprefix("http://")
    conn = http.client.HTTPConnection(hostport, timeout=5)
    conn.putrequest("GET", "/api/dashboard", skip_host=True)
    conn.putheader("Host", host)
    conn.endheaders()
    assert conn.getresponse().status == 421
    conn.close()


@pytest.mark.parametrize("path", ["/sub/", "/.secret", "/%2esecret", "/sub/.hidden", "/DESIGN.md"])
def test_servers_hide_listings_and_dotfiles(either, path):
    assert get(either + path)[0] == 404


# --- the real server over saved snapshots -------------------------------------


@contextlib.contextmanager
def live_server(home, web=None):
    """Start the REAL dashboard server over ``home``; yield its base URL."""
    server, _thread, url = seed.start_server(home, web_directory=web)
    try:
        yield url
    finally:
        seed.stop_server(server)


def fetch_view(url):
    status, headers, body = get(url + "/api/dashboard")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    return json.loads(body)


def test_live_empty_store_is_never(tmp_path):
    home = tmp_path
    seed.write_test_config(home, server_port=1)
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view)
    assert view["modules"][0]["status"] == "never"
    assert view["unifi_wifi"] is None


def test_live_one_snapshot_has_no_previous(tmp_path):
    home = tmp_path
    ids = seed.seed_snapshots(home, count=1, start=START)
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert view["unifi_wifi"]["snapshots"]["total"] == 1
    assert view["unifi_wifi"]["latest"]["id"] == ids[0]
    assert view["unifi_wifi"]["previous"] is None


def test_live_two_snapshots_set_previous(tmp_path):
    home = tmp_path
    seed.seed_snapshots(home, count=2, start=START)
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert view["unifi_wifi"]["snapshots"]["total"] == 2
    assert view["unifi_wifi"]["previous"] is not None


def test_live_recent_capped_at_50_with_exact_total(tmp_path):
    home = tmp_path
    seed.seed_snapshots(home, count=55, start=START)
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert view["unifi_wifi"]["snapshots"]["total"] == 55
    assert len(view["unifi_wifi"]["snapshots"]["recent"]) == 50


def test_live_newest_corrupt_file_is_skipped(tmp_path):
    home = tmp_path
    ids = seed.seed_snapshots(home, count=2, start=START)
    (seed.module_dir(home) / (ids[1] + ".json")).write_bytes(b"{broken")
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert view["unifi_wifi"]["snapshots"]["total"] == 1
    assert view["unifi_wifi"]["latest"]["id"] == ids[0]


def test_live_old_schema_file_is_skipped(tmp_path):
    home = tmp_path
    ids = seed.seed_snapshots(home, count=1, start=START)
    stale = seed.module_dir(home) / "snapshot_20260101T000000Z_0000.json"
    stale.write_text(json.dumps({
        "schema": "housesitter.unifi_wifi.snapshot/0",
        "module": "unifi_wifi",
        "collected_at": "2026-01-01T00:00:00Z",
        "data": {},
        "source": {},
        "warnings": [],
    }))
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert view["unifi_wifi"]["snapshots"]["total"] == 1
    assert view["unifi_wifi"]["latest"]["id"] == ids[0]


def test_live_all_corrupt_is_error_with_null_data(tmp_path):
    home = tmp_path
    seed.write_test_config(home, server_port=1)
    directory = seed.module_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    for suffix in ("aaaa", "bbbb"):
        (directory / f"snapshot_20261101T120000Z_{suffix}.json").write_bytes(
            b"not json at all"
        )
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view)
    entry = view["modules"][0]
    assert entry["status"] == "error"
    assert view["unifi_wifi"] is None


def test_live_disabled_module_has_no_entry(tmp_path):
    home = tmp_path
    seed.seed_snapshots(home, count=1, start=START)
    seed.write_test_config(home, server_port=1, enabled=False)
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view)
    assert view["modules"] == []
    assert view["unifi_wifi"] is None


def test_live_hostile_names_are_data_sanitised(tmp_path):
    home = tmp_path
    seed.seed_snapshots(home, count=1, start=START, routes_kind="hostile")
    with live_server(home) as url:
        view = fetch_view(url)
    check_view(view, realistic=False)
    assert "PLANTED-SECRET" not in json.dumps(view)


def test_live_identifiers_none_strips_ids(tmp_path):
    home = tmp_path
    seed.seed_snapshots(home, count=1, start=START, identifiers="none")
    with live_server(home) as url:
        view = fetch_view(url)
    dumped = json.dumps(view["unifi_wifi"])
    assert "02:00" not in dumped
    assert "192.0.2" not in dumped
    assert view["unifi_wifi"]["snapshots"]["total"] == 1
    # D24: the keys stay, with null values, so the exact key sets hold.
    check_view(view, realistic=False)
    assert ids_hidden(view["unifi_wifi"]["latest"])


@pytest.mark.parametrize("path", ["/%2e%2e/", "/..%2f", "/web/../pyproject.toml"])
def test_live_rejects_encoded_traversal(live, path):
    assert get(live + path)[0] == 404


def test_live_error_responses_carry_security_headers(live):
    marker = "MARKER-never-echo-12345"
    wanted = ("Cache-Control", "X-Content-Type-Options",
              "Content-Security-Policy", "Referrer-Policy")
    for probe in (f"/{marker}", "/api/dashboard", "/"):
        status, headers, body = get(live + probe, method="POST")
        assert status == 405
        for name in wanted:
            assert name in headers, (probe, name)
        assert marker.encode() not in body
    status, headers, body = get(live + f"/{marker}")
    assert status == 404
    for name in wanted:
        assert name in headers, name
    assert marker.encode() not in body
    assert body == b"not found"
    import http.client

    hostport = live.removeprefix("http://")
    conn = http.client.HTTPConnection(hostport, timeout=5)
    conn.putrequest("GET", "/api/dashboard", skip_host=True)
    conn.putheader("Host", "evil.example")
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 421
    lowered = {key.lower(): value for key, value in resp.getheaders()}
    assert lowered.get("cache-control") == "no-store"
    assert "content-security-policy" in lowered
    assert b"evil.example" not in resp.read()
    conn.close()


def test_live_unavailable_store_is_503_with_headers(tmp_path, live_web):
    home = tmp_path
    seed.write_test_config(home, server_port=1)
    root = home / ".local" / "share" / "housesitter"
    root.mkdir(parents=True)
    target = home / "elsewhere"
    target.mkdir()
    root.rename(home / ".local" / "share" / "housesitter-real")
    os.symlink(target, root)
    try:
        with live_server(home, web=live_web) as url:
            status, headers, body = get(url + "/api/dashboard")
    finally:
        os.unlink(root)
    assert status == 503
    assert json.loads(body) == {"error": "dashboard data unavailable"}
    for name in ("Cache-Control", "X-Content-Type-Options",
                 "Content-Security-Policy", "Referrer-Policy"):
        assert name in headers, name
