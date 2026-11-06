"""The dashboard view-model contract (docs/DASHBOARD.md).

Pins the exact keys of every object in housesitter.dashboard/1, checks the
committed fixtures against it and against their generator, and smoke-tests
the development server.
"""

from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "dashboard"


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
        if client["ap_id"] is None:
            assert client["ap_name"] is None
    ids = [c["id"] for c in snap["clients"]]
    assert len(ids) == len(set(ids)), "client ids must be unique within a snapshot"
    check_summary(snap, aps)
    if realistic:
        check_controller_counts(snap, aps)


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
def test_dev_server_is_read_only(server, method):
    assert get(server + "/api/dashboard", method=method)[0] == 405


def test_dev_server_answers_head_for_the_api(server):
    status, headers, body = get(server + "/api/dashboard", method="HEAD")
    assert status == 200 and body == b""
    assert int(headers["Content-Length"]) > 0


def test_dev_server_sends_the_documented_csp(server):
    csp = get(server + "/")[1]["Content-Security-Policy"]
    assert csp == dev_dashboard.CSP
    for directive in ("script-src 'self'", "form-action 'none'", "frame-ancestors 'none'",
                      "require-trusted-types-for 'script'"):
        assert directive in csp
    doc = (ROOT / "docs" / "DASHBOARD.md").read_text(encoding="utf-8")
    assert " ".join(doc.split()).count(csp) == 1, "DASHBOARD.md §5 must quote the same CSP"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "127.0.0.1.evil.example"])
def test_dev_server_refuses_foreign_host_headers(server, host):
    import http.client

    hostport = server.removeprefix("http://")
    conn = http.client.HTTPConnection(hostport, timeout=5)
    conn.putrequest("GET", "/api/dashboard", skip_host=True)
    conn.putheader("Host", host)
    conn.endheaders()
    assert conn.getresponse().status == 421
    conn.close()


@pytest.mark.parametrize("path", ["/sub/", "/.secret", "/%2esecret", "/sub/.hidden"])
def test_dev_server_hides_listings_and_dotfiles(server, path):
    assert get(server + path)[0] == 404
