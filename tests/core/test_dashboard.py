"""Pin the read-only dashboard view (docs/DASHBOARD.md §4).

Every store here holds REAL saved snapshots produced through the real
runner against a loopback fixture server (``tests/dashboard_seed.py``),
so the view builder sees exactly what the server will see. All data is
invented; ``tests/test_hygiene.py`` must stay green.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dashboard_seed as seed
from housesitter.core.cli import main as cli_main
from housesitter.core.config import ConfigError, load_config
from housesitter.core.dashboard import DashboardUnavailable, build_view, clear_cache
from housesitter.core.registry import Registry
from housesitter.modules.unifi_wifi import UniFiWifi

START = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def registry() -> Registry:
    return Registry.from_classes([UniFiWifi])


def view_for(home: Path, **kwargs: object) -> dict:
    reg = registry()
    config = load_config(reg, env={"HOME": str(home)})
    return build_view(registry=reg, config=config,
                      env={"HOME": str(home)}, **kwargs)


def all_text(value: object) -> list:
    found: list = []

    def walk(item: object) -> None:
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            for entry in item.values():
                walk(entry)
        elif isinstance(item, (list, tuple)):
            for entry in item:
                walk(entry)

    walk(value)
    return found


# -- config: the housesitter.dashboard section -------------------------------


class TestDashboardConfig:
    def test_defaults(self, tmp_path):
        seed.write_test_config(tmp_path)
        config = load_config(registry(), env={"HOME": str(tmp_path)})
        assert config.dashboard.bind == "127.0.0.1"
        assert config.dashboard.port == 8080
        assert config.dashboard.allowed_hosts == ()
        assert config.dashboard.house_label is None

    def test_explicit_values(self, tmp_path):
        seed.write_test_config(tmp_path, dashboard={
            "bind": "0.0.0.0",
            "port": 8123,
            "allowed_hosts": ["Pi-Host.example", "198.51.100.7"],
            "house_label": "Example house",
        })
        dashboard = load_config(
            registry(), env={"HOME": str(tmp_path)}
        ).dashboard
        assert dashboard.bind == "0.0.0.0"
        assert dashboard.port == 8123
        # Compared lowercase: stored lowered.
        assert dashboard.allowed_hosts == ("pi-host.example", "198.51.100.7")
        assert dashboard.house_label == "Example house"

    def test_ipv6_binds_allowed(self, tmp_path):
        for bind in ("::", "::1"):
            seed.write_test_config(tmp_path, dashboard={"bind": bind})
            assert load_config(
                registry(), env={"HOME": str(tmp_path)}
            ).dashboard.bind == bind

    @pytest.mark.parametrize("bind", ["example.com", "999.0.0.1", "", 8080, None])
    def test_bad_bind_rejected(self, tmp_path, bind):
        seed.write_test_config(tmp_path, dashboard={"bind": bind})
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry(), env={"HOME": str(tmp_path)})
        assert excinfo.value.key == "housesitter.dashboard.bind"
        assert "MARKER" not in str(excinfo.value)

    @pytest.mark.parametrize("port", [0, 65536, -1, True, False, 80.5, "8080"])
    def test_bad_port_rejected(self, tmp_path, port):
        seed.write_test_config(tmp_path, dashboard={"port": port})
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry(), env={"HOME": str(tmp_path)})
        assert excinfo.value.key == "housesitter.dashboard.port"

    def test_port_edges_ok(self, tmp_path):
        for port in (1, 65535):
            seed.write_test_config(tmp_path, dashboard={"port": port})
            assert load_config(
                registry(), env={"HOME": str(tmp_path)}
            ).dashboard.port == port

    def test_bad_host_rejected_without_echo(self, tmp_path):
        seed.write_test_config(
            tmp_path, dashboard={"allowed_hosts": ["bad_host_MARKER!"]}
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry(), env={"HOME": str(tmp_path)})
        assert excinfo.value.key == "housesitter.dashboard.allowed_hosts"
        assert "MARKER" not in str(excinfo.value)

    def test_too_many_hosts_rejected(self, tmp_path):
        seed.write_test_config(
            tmp_path,
            dashboard={"allowed_hosts": [f"host{i}.example" for i in range(33)]},
        )
        with pytest.raises(ConfigError):
            load_config(registry(), env={"HOME": str(tmp_path)})

    def test_32_hosts_ok(self, tmp_path):
        hosts = [f"host{i}.example" for i in range(32)]
        seed.write_test_config(tmp_path, dashboard={"allowed_hosts": hosts})
        assert load_config(
            registry(), env={"HOME": str(tmp_path)}
        ).dashboard.allowed_hosts == tuple(hosts)

    def test_bad_label_rejected_without_echo(self, tmp_path):
        seed.write_test_config(
            tmp_path, dashboard={"house_label": "MARKER" + "x" * 70}
        )
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry(), env={"HOME": str(tmp_path)})
        assert excinfo.value.key == "housesitter.dashboard.house_label"
        assert "MARKER" not in str(excinfo.value)

    def test_unknown_sub_key_rejected(self, tmp_path):
        seed.write_test_config(tmp_path, dashboard={"frobnicate": 1})
        with pytest.raises(ConfigError) as excinfo:
            load_config(registry(), env={"HOME": str(tmp_path)})
        assert excinfo.value.key == "housesitter.dashboard.frobnicate"

    def test_config_check_validates_dashboard(self, tmp_path):
        seed.write_test_config(tmp_path, dashboard={"port": 0})
        out, err = io.StringIO(), io.StringIO()
        code = cli_main(
            ["config-check"], env={"HOME": str(tmp_path)},
            stdout=out, stderr=err, registry=registry(),
        )
        assert code == 1
        assert "config_invalid" in err.getvalue()
        assert "housesitter.dashboard.port" in err.getvalue()


# -- view states --------------------------------------------------------------


class TestViewStates:
    def test_empty_store_is_never(self, tmp_path):
        seed.write_test_config(tmp_path, server_port=1)
        view = view_for(tmp_path)
        assert view["schema"] == "housesitter.dashboard/1"
        assert view["muse_note"] is None
        assert view["house_label"] is None
        assert view["modules"] == [{
            "id": "unifi_wifi",
            "title": "UniFi WiFi radio metrics",
            "version": "0.1.0",
            "status": "never",
            "last_success_at": None,
            "last_attempt_at": None,
            "last_error": None,
        }]
        assert view["unifi_wifi"] is None

    def test_missing_data_dir_never_creates_it(self, tmp_path):
        seed.write_test_config(tmp_path, server_port=1)
        data_root = tmp_path / ".local" / "share" / "housesitter"
        assert not data_root.exists()
        view = view_for(tmp_path)
        assert view["unifi_wifi"] is None
        assert not data_root.exists()

    def test_one_snapshot_has_no_previous(self, tmp_path):
        home = tmp_path
        ids = seed.seed_snapshots(home, count=1, start=START)
        view = view_for(home)
        entry = view["modules"][0]
        assert entry["status"] == "ok"
        assert entry["last_success_at"] == "2026-11-01T12:00:00Z"
        assert entry["last_attempt_at"] == "2026-11-01T12:00:00Z"
        assert entry["last_error"] is None
        unifi = view["unifi_wifi"]
        assert unifi["snapshots"]["total"] == 1
        assert len(unifi["snapshots"]["recent"]) == 1
        assert unifi["latest"]["id"] == ids[0]
        assert unifi["previous"] is None

    def test_two_snapshots_set_previous(self, tmp_path):
        ids = seed.seed_snapshots(tmp_path, count=2, start=START)
        view = view_for(tmp_path)
        unifi = view["unifi_wifi"]
        assert unifi["snapshots"]["total"] == 2
        assert [item["id"] for item in unifi["snapshots"]["recent"]] == [
            ids[1], ids[0],
        ]
        assert unifi["latest"]["id"] == ids[1]
        assert unifi["previous"]["id"] == ids[0]
        entry = view["modules"][0]
        assert entry["last_success_at"] == "2026-11-01T12:01:00Z"

    def test_recent_capped_at_50_with_exact_total(self, tmp_path):
        ids = seed.seed_snapshots(tmp_path, count=55, start=START)
        view = view_for(tmp_path)
        unifi = view["unifi_wifi"]
        assert unifi["snapshots"]["total"] == 55
        recent = unifi["snapshots"]["recent"]
        assert len(recent) == 50
        assert [item["id"] for item in recent] == ids[-1:-51:-1]
        assert unifi["latest"]["id"] == ids[-1]
        assert unifi["previous"]["id"] == ids[-2]

    def test_newest_corrupt_file_is_skipped(self, tmp_path, capsys):
        ids = seed.seed_snapshots(tmp_path, count=3, start=START)
        (seed.module_dir(tmp_path) / (ids[2] + ".json")).write_bytes(
            b"{broken json"
        )
        view = view_for(tmp_path)
        unifi = view["unifi_wifi"]
        assert unifi["snapshots"]["total"] == 2
        assert unifi["latest"]["id"] == ids[1]
        assert unifi["previous"]["id"] == ids[0]
        assert view["modules"][0]["status"] == "ok"
        logged = capsys.readouterr().err
        assert ids[2] in logged
        assert "{broken" not in logged

    def test_old_schema_file_is_skipped(self, tmp_path, capsys):
        ids = seed.seed_snapshots(tmp_path, count=1, start=START)
        stale = (
            seed.module_dir(tmp_path) / "snapshot_20260101T000000Z_0000.json"
        )
        stale.write_text(json.dumps({
            "schema": "housesitter.unifi_wifi.snapshot/0",
            "module": "unifi_wifi",
            "collected_at": "2026-01-01T00:00:00Z",
            "data": {},
            "source": {},
            "warnings": [],
        }))
        view = view_for(tmp_path)
        unifi = view["unifi_wifi"]
        assert unifi["snapshots"]["total"] == 1
        assert unifi["latest"]["id"] == ids[0]
        assert "snapshot_20260101T000000Z_0000" in capsys.readouterr().err

    def test_all_corrupt_is_error_with_null_data(self, tmp_path):
        seed.write_test_config(tmp_path, server_port=1)
        directory = seed.module_dir(tmp_path)
        directory.mkdir(parents=True, exist_ok=True)
        for suffix in ("aaaa", "bbbb"):
            (directory / f"snapshot_20261101T120000Z_{suffix}.json").write_bytes(
                b"not json at all"
            )
        view = view_for(tmp_path)
        entry = view["modules"][0]
        assert entry["status"] == "error"
        assert entry["last_success_at"] is None
        assert entry["last_attempt_at"] is None
        assert entry["last_error"] == {
            "code": "upstream_unexpected",
            "message": "Saved snapshots could not be read.",
        }
        assert view["unifi_wifi"] is None

    def test_disabled_module_has_no_entry_and_null_key(self, tmp_path):
        seed.seed_snapshots(tmp_path, count=1, start=START)
        seed.write_test_config(tmp_path, server_port=1, enabled=False)
        view = view_for(tmp_path)
        assert view["modules"] == []
        assert view["unifi_wifi"] is None

    def test_shared_collected_at_keeps_one(self, tmp_path):
        ids = seed.seed_snapshots(
            tmp_path, count=2, start=START, step_s=0
        )
        view = view_for(tmp_path)
        unifi = view["unifi_wifi"]
        # Both saves landed in the same second, so they share a
        # collected-at: the contract needs strictly descending unique
        # times, so only the newest file shows and total agrees.
        assert unifi["snapshots"]["total"] == 1
        assert len(unifi["snapshots"]["recent"]) == 1
        assert unifi["previous"] is None
        assert unifi["latest"]["id"] in set(ids)

    def test_house_label_is_sanitised(self, tmp_path):
        seed.seed_snapshots(tmp_path, count=1, start=START)
        seed.write_test_config(
            tmp_path,
            server_port=1,
            dashboard={"house_label": "  My\x00House\u200b  "},
        )
        view = view_for(tmp_path)
        assert view["house_label"] == "MyHouse"

    def test_unsafe_data_dir_raises(self, tmp_path):
        seed.seed_snapshots(tmp_path, count=1, start=START)
        root = tmp_path / ".local" / "share" / "housesitter"
        target = tmp_path / "elsewhere"
        target.mkdir()
        root.rename(tmp_path / ".local" / "share" / "housesitter-real")
        try:
            os.symlink(target, root)
            with pytest.raises(DashboardUnavailable):
                view_for(tmp_path)
        finally:
            os.unlink(root)

    def test_second_build_after_corruption_drops_total(self, tmp_path):
        ids = seed.seed_snapshots(tmp_path, count=2, start=START)
        assert view_for(tmp_path)["unifi_wifi"]["snapshots"]["total"] == 2
        newest = seed.module_dir(tmp_path) / (ids[1] + ".json")
        newest.write_bytes(b"corrupt")
        view = view_for(tmp_path)
        assert view["unifi_wifi"]["snapshots"]["total"] == 1
        assert view["unifi_wifi"]["latest"]["id"] == ids[0]


# -- data handling ------------------------------------------------------------


class TestDataHandling:
    def test_hostile_names_are_data_sanitised(self, tmp_path):
        seed.seed_snapshots(
            tmp_path, count=1, start=START, routes_kind="hostile"
        )
        view = view_for(tmp_path)
        latest = view["unifi_wifi"]["latest"]
        assert view["unifi_wifi"]["snapshots"]["total"] == 1
        # Hostile text survives as data (twins share a name), never dropped.
        names = [item["name"] for item in latest["clients"]]
        assert names.count("Twin") == 2
        assert "PLANTED-SECRET" not in json.dumps(view)
        for text in all_text(view["unifi_wifi"]):
            assert len(text) <= 1400, text[:40]
            for char in text:
                assert unicodedata.category(char) not in ("Cc", "Cf", "Cs"), (
                    repr(text[:60])
                )

    def test_identifiers_none_strips_ids_like_invoke(self, tmp_path):
        seed.seed_snapshots(
            tmp_path, count=1, start=START, identifiers="none"
        )
        view = view_for(tmp_path)
        assert view["unifi_wifi"]["snapshots"]["total"] == 1
        dumped = json.dumps(view["unifi_wifi"])
        assert "02:00" not in dumped
        assert "00:00:5e" not in dumped
        assert "192.0.2" not in dumped
        assert "198.51.100" not in dumped
        assert "203.0.113" not in dumped
        latest = view["unifi_wifi"]["latest"]
        # The store id is envelope-level, not an identifier field: it stays.
        assert latest["id"].startswith("snapshot_")
        # D24: hidden identifiers stay as null-valued keys (exact key sets).
        for client in latest["clients"]:
            assert client["id"] is None
            assert client["ip"] is None
            assert client["ap_id"] is None
        for ap in latest["aps"]:
            assert ap["id"] is None
        for entry in latest["summary"]["clients_by_ap"]:
            assert entry["ap_id"] is None

    def test_never_writes(self, tmp_path):
        home = tmp_path
        seed.seed_snapshots(home, count=3, start=START)

        def snapshot_tree() -> dict:
            found = {}
            for path in sorted(home.rglob("*")):
                st = os.lstat(path)
                digest = None
                if stat.S_ISREG(st.st_mode):
                    digest = hashlib.sha256(
                        path.read_bytes()
                    ).hexdigest()
                found[str(path.relative_to(home))] = (
                    stat.S_IMODE(st.st_mode), st.st_mtime_ns,
                    st.st_size, digest,
                )
            return found

        before = snapshot_tree()
        for _ in range(3):
            view_for(home)
        assert snapshot_tree() == before

    def test_no_upstream_contact_while_building(self, tmp_path, monkeypatch):
        import socket as socket_mod

        seed.seed_snapshots(tmp_path, count=2, start=START)

        def refuse(*args: object, **kwargs: object) -> None:
            raise AssertionError("dashboard must not open sockets")

        monkeypatch.setattr(socket_mod.socket, "connect", refuse)
        monkeypatch.setattr(socket_mod, "create_connection", refuse)
        view = view_for(tmp_path)
        assert view["unifi_wifi"]["snapshots"]["total"] == 2


# -- cache: hits never re-read files; the identifier policy is part of the key


def test_cached_files_are_not_reread_on_later_requests(tmp_path, monkeypatch):
    from housesitter.core.store import FileStore

    seed.seed_snapshots(tmp_path, count=5, start=START)
    first = view_for(tmp_path)
    loads: list = []
    real_load = FileStore.load

    def counting_load(self, id):
        loads.append(id)
        return real_load(self, id)

    monkeypatch.setattr(FileStore, "load", counting_load)
    second = view_for(tmp_path)
    assert loads == []
    assert second["unifi_wifi"] == first["unifi_wifi"]


def test_switching_to_identifiers_none_never_serves_cached_ids(tmp_path):
    seed.seed_snapshots(tmp_path, count=2, start=START)
    full = json.dumps(view_for(tmp_path)["unifi_wifi"])
    assert "02:00" in full or "00:00:5e" in full
    seed.write_test_config(tmp_path, identifiers="none")
    dumped = json.dumps(view_for(tmp_path)["unifi_wifi"])
    for marker in ("02:00", "00:00:5e", "192.0.2", "198.51.100", "203.0.113"):
        assert marker not in dumped


def test_file_pruned_between_scan_and_resolution_is_dropped(tmp_path, monkeypatch):
    """A concurrent save's retention prune can delete a file after the scan
    listed it: latest/previous/recent/total must still agree (contract)."""
    import importlib.util as _ilu

    from housesitter.core import dashboard as dash

    seed.seed_snapshots(tmp_path, count=3, start=START)
    warm = view_for(tmp_path)
    newest = warm["unifi_wifi"]["latest"]["id"]
    with dash._cache_lock:
        dash._full_cache.clear()  # force resolution to reload latest
    real = dash._load_validated
    calls: dict = {}

    def racing(**kwargs):
        if kwargs["store_id"] == newest:
            calls[newest] = calls.get(newest, 0) + 1
            if calls[newest] == 2:  # the resolution-time reload
                (seed.module_dir(tmp_path) / f"{newest}.json").unlink()
        return real(**kwargs)

    monkeypatch.setattr(dash, "_load_validated", racing)
    view = view_for(tmp_path)
    assert calls[newest] == 2
    unifi = view["unifi_wifi"]
    assert unifi["snapshots"]["total"] == 2
    assert newest not in [entry["id"] for entry in unifi["snapshots"]["recent"]]
    spec = _ilu.spec_from_file_location(
        "contract", Path(__file__).resolve().parents[1] / "test_dashboard_contract.py"
    )
    contract = _ilu.module_from_spec(spec)
    spec.loader.exec_module(contract)
    contract.check_view(view, realistic=False)


def test_cache_drops_entries_for_files_no_longer_listed(tmp_path):
    from housesitter.core import dashboard as dash

    ids = seed.seed_snapshots(tmp_path, count=3, start=START)
    view_for(tmp_path)
    (seed.module_dir(tmp_path) / f"{ids[0]}.json").unlink()
    view_for(tmp_path)
    with dash._cache_lock:
        cached = {key[0] for key in dash._history_cache}
    assert ids[0] not in cached and set(ids[1:]) <= cached
