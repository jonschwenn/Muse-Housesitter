"""Pin snapshot storage: atomic saves, modes, retention, paging, load."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from housesitter import sdk
from housesitter.core.store import FileStore, StoreError, data_root


def utc(second: int = 0) -> datetime:
    return datetime(2026, 11, 4, 15, 15, second, tzinfo=timezone.utc)


def clock_over(*seconds: int):
    ticks = [utc(second) for second in seconds]
    return iter(ticks).__next__


def suffixes(*values: str):
    return iter(list(values)).__next__


def make_store(tmp_path: Path, module_id: str = "demo_sensor", **kwargs) -> FileStore:
    env = {"HOME": str(tmp_path)}
    return FileStore(module_id, env=env, **kwargs)


class TestDataRoot:
    def test_derives_from_home(self, tmp_path):
        assert data_root({"HOME": str(tmp_path)}) == (
            tmp_path / ".local" / "share" / "housesitter"
        )

    def test_init_and_reads_create_nothing(self, tmp_path):
        store = make_store(tmp_path)
        assert store.list("snapshot").total == 0
        with pytest.raises(StoreError) as excinfo:
            store.load("snapshot_20261104T151500Z_0001")
        assert excinfo.value.code == "not_found"
        assert not (tmp_path / ".local").exists()

    def test_creates_mode_700(self, tmp_path):
        store = make_store(tmp_path)
        store.save("snapshot", {"n": 1})
        mode = stat.S_IMODE(os.lstat(store.module_dir).st_mode)
        assert mode == 0o700
        root_mode = stat.S_IMODE(os.lstat(store.module_dir.parent).st_mode)
        assert root_mode == 0o700

    def test_fixes_wide_module_dir(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        module_dir = root / "demo_sensor"
        module_dir.mkdir(parents=True)
        os.chmod(module_dir, 0o755)
        store = make_store(tmp_path)
        store.save("snapshot", {"n": 1})
        assert stat.S_IMODE(os.lstat(store.module_dir).st_mode) == 0o700

    def test_save_tightens_wide_data_root(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        root.mkdir(parents=True)
        os.chmod(root, 0o755)
        store = make_store(tmp_path)
        store.save("snapshot", {"n": 1})
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(store.module_dir).st_mode) == 0o700

    def test_list_never_chmods(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        module_dir = root / "demo_sensor"
        module_dir.mkdir(parents=True)
        os.chmod(root, 0o755)
        os.chmod(module_dir, 0o755)
        (module_dir / "snapshot_20261104T151500Z_0001.json").write_bytes(
            b'{"n":1}'
        )
        store = make_store(tmp_path)
        assert store.list("snapshot").total == 1
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o755
        assert stat.S_IMODE(os.lstat(module_dir).st_mode) == 0o755

    def test_load_never_chmods(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        module_dir = root / "demo_sensor"
        module_dir.mkdir(parents=True)
        os.chmod(root, 0o755)
        os.chmod(module_dir, 0o755)
        (module_dir / "snapshot_20261104T151500Z_0001.json").write_bytes(
            b'{"n":1}'
        )
        store = make_store(tmp_path)
        assert store.load("snapshot_20261104T151500Z_0001") == {"n": 1}
        assert stat.S_IMODE(os.lstat(root).st_mode) == 0o755
        assert stat.S_IMODE(os.lstat(module_dir).st_mode) == 0o755

    def test_symlinked_module_dir_refused(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        root.mkdir(parents=True)
        (tmp_path / "real").mkdir()
        os.symlink(tmp_path / "real", root / "demo_sensor")
        with pytest.raises(StoreError) as excinfo:
            make_store(tmp_path).save("snapshot", {"n": 1})
        assert excinfo.value.code == "config_invalid"
        assert "doctor" in excinfo.value.reason

    def test_symlinked_root_refused(self, tmp_path):
        root = tmp_path / ".local" / "share" / "housesitter"
        root.parent.mkdir(parents=True)
        (tmp_path / "real").mkdir()
        os.symlink(tmp_path / "real", root)
        with pytest.raises(StoreError) as excinfo:
            make_store(tmp_path).list("snapshot")
        assert excinfo.value.code == "config_invalid"

    def test_bad_module_id_refused(self, tmp_path):
        with pytest.raises(ValueError):
            make_store(tmp_path, module_id="../evil")

    def test_bad_retention_refused(self, tmp_path):
        with pytest.raises(ValueError):
            make_store(tmp_path, retention=-1)


class TestSave:
    def test_round_trip(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("3fa1"))
        saved_id = store.save("snapshot", {"a": 1}, "before")
        assert saved_id == "snapshot_20261104T151500Z_3fa1"
        assert re.fullmatch(
            r"[a-z][a-z0-9_]{0,31}_\d{8}T\d{6}Z_[0-9a-f]{4}", saved_id
        )
        assert store.load(saved_id) == {"a": 1}

    def test_file_mode_600_and_compact(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"a": 1, "b": [1, 2]})
        path = store.module_dir / (saved_id + ".json")
        assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
        assert path.read_bytes() == b'{"a":1,"b":[1,2]}'

    def test_no_temp_files_left(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        store.save("snapshot", {"a": 1})
        names = os.listdir(store.module_dir)
        assert names == ["snapshot_20261104T151500Z_0001.json"]

    def test_nan_is_not_json_serialisable(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.save("snapshot", {"n": float("nan")})

    def test_infinite_is_not_json_serialisable(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.save("snapshot", {"n": [float("inf")]})

    def test_module_dir_is_fsynced_after_save(self, tmp_path, monkeypatch):
        import housesitter.core.store as store_mod

        synced = []
        real_fsync = os.fsync

        def spy_fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(store_mod.os, "fsync", spy_fsync)
        store = make_store(tmp_path)
        saved_id = store.save("snapshot", {"n": 1})
        assert saved_id
        assert synced, "save must fsync (the file, and the module dir)"

    def test_dir_fsync_failure_still_saves(self, tmp_path, monkeypatch):
        import housesitter.core.store as store_mod

        real_fsync = os.fsync
        dir_calls = []

        def flaky_fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                dir_calls.append(fd)
                raise OSError("filesystem refuses fsync")
            return real_fsync(fd)

        monkeypatch.setattr(store_mod.os, "fsync", flaky_fsync)
        store = make_store(tmp_path)
        saved_id = store.save("snapshot", {"n": 1})
        assert store.load(saved_id) == {"n": 1}
        assert dir_calls, "the module dir fsync must still be attempted"

    def test_stale_tmp_files_cleaned_after_save(self, tmp_path):
        fixed = datetime(2026, 11, 4, 15, 15, 0, tzinfo=timezone.utc)
        store = make_store(tmp_path, clock=lambda: fixed)
        module_dir = tmp_path / ".local" / "share" / "housesitter" / "demo_sensor"
        module_dir.mkdir(parents=True)
        stale = module_dir / ".tmp-old-1234.json"
        stale.write_bytes(b"{}")
        old = fixed.timestamp() - 7200
        os.utime(stale, (old, old))
        fresh = module_dir / ".tmp-new-5678.json"
        fresh.write_bytes(b"{}")
        new = fixed.timestamp() - 60
        os.utime(fresh, (new, new))
        link = module_dir / ".tmp-link-9abc.json"
        os.symlink(stale, link)
        subdir = module_dir / ".tmp-dir-def0.json"
        subdir.mkdir()
        other = module_dir / "notes.tmp-x.json"
        other.write_text("keep me")
        store.save("snapshot", {"n": 1})
        leftovers = sorted(os.listdir(module_dir))
        assert ".tmp-old-1234.json" not in leftovers
        assert ".tmp-new-5678.json" in leftovers
        assert fresh.exists()
        assert os.path.islink(link)
        assert subdir.is_dir()
        assert other.read_text() == "keep me"

    def test_collision_retries_with_new_suffix(self, tmp_path):
        store = make_store(
            tmp_path, clock=clock_over(0, 0), suffix=suffixes("aaaa", "aaaa", "bbbb")
        )
        first = store.save("snapshot", {"n": 1})
        second = store.save("snapshot", {"n": 2})
        assert first == "snapshot_20261104T151500Z_aaaa"
        assert second == "snapshot_20261104T151500Z_bbbb"
        assert store.load(first) == {"n": 1}
        assert store.load(second) == {"n": 2}

    def test_bad_kind_refused(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.save("Snapshot", {})

    def test_retention_prunes_oldest(self, tmp_path):
        store = make_store(
            tmp_path,
            retention=2,
            clock=clock_over(0, 1, 2),
            suffix=suffixes("0001", "0002", "0003"),
        )
        store.save("snapshot", {"n": 1})
        store.save("snapshot", {"n": 2})
        store.save("snapshot", {"n": 3})
        names = sorted(os.listdir(store.module_dir))
        assert names == [
            "snapshot_20261104T151501Z_0002.json",
            "snapshot_20261104T151502Z_0003.json",
        ]

    def test_retention_zero_keeps_everything(self, tmp_path):
        store = make_store(
            tmp_path,
            retention=0,
            clock=clock_over(0, 1, 2),
            suffix=suffixes("0001", "0002", "0003"),
        )
        for n in range(3):
            store.save("snapshot", {"n": n})
        assert len(os.listdir(store.module_dir)) == 3

    def test_retention_counts_kinds_together(self, tmp_path):
        store = make_store(
            tmp_path,
            retention=2,
            clock=clock_over(0, 1, 2),
            suffix=suffixes("0001", "0002", "0003"),
        )
        store.save("snapshot", {"n": 1})
        store.save("other", {"n": 2})
        store.save("snapshot", {"n": 3})
        remaining = sorted(os.listdir(store.module_dir))
        assert len(remaining) == 2
        assert "snapshot_20261104T151500Z_0001.json" not in remaining

    def test_prune_ignores_foreign_files(self, tmp_path):
        store = make_store(
            tmp_path, retention=1, clock=clock_over(0, 1), suffix=suffixes("0001", "0002")
        )
        store.save("snapshot", {"n": 1})
        (store.module_dir / "notes.txt").write_text("keep me")
        store.save("snapshot", {"n": 2})
        assert (store.module_dir / "notes.txt").read_text() == "keep me"

    def test_concurrent_saves_produce_distinct_files(self, tmp_path):
        store = make_store(tmp_path)
        ids: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            saved_id = store.save("snapshot", {"thread": True})
            with lock:
                ids.append(saved_id)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert len(set(ids)) == 8
        assert len(os.listdir(store.module_dir)) == 8


class TestList:
    def fill(self, tmp_path: Path, count: int = 5) -> FileStore:
        store = make_store(
            tmp_path,
            retention=0,
            clock=clock_over(*range(count + 1)),
            suffix=suffixes(*[f"{n:04x}" for n in range(count + 1)]),
        )
        for n in range(count):
            store.save(
                "snapshot",
                {
                    "n": n,
                    "collected_at": f"2026-11-04T15:15:0{n}Z",
                    "label": f"reading {n}",
                },
            )
        return store

    def test_newest_first_with_total(self, tmp_path):
        store = self.fill(tmp_path)
        page = store.list("snapshot")
        assert page.total == 5
        assert [item.id for item in page.items] == [
            "snapshot_20261104T151504Z_0004",
            "snapshot_20261104T151503Z_0003",
            "snapshot_20261104T151502Z_0002",
            "snapshot_20261104T151501Z_0001",
            "snapshot_20261104T151500Z_0000",
        ]
        assert isinstance(page, sdk.StorePage)

    def test_paging(self, tmp_path):
        store = self.fill(tmp_path)
        page = store.list("snapshot", offset=1, limit=2)
        assert page.total == 5
        assert [item.id for item in page.items] == [
            "snapshot_20261104T151503Z_0003",
            "snapshot_20261104T151502Z_0002",
        ]

    def test_headers_read_from_files(self, tmp_path):
        store = self.fill(tmp_path, 1)
        (item,) = store.list("snapshot").items
        assert item.kind == "snapshot"
        assert item.collected_at == "2026-11-04T15:15:00Z"
        assert item.label == "reading 0"

    def test_non_string_headers_become_none(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save(
            "snapshot", {"collected_at": 123, "label": ["x"]},
        )
        (item,) = store.list("snapshot").items
        assert item.id == saved_id
        assert item.collected_at is None
        assert item.label is None

    def test_damaged_headers_become_none(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"n": 1})
        (store.module_dir / (saved_id + ".json")).write_bytes(
            b'{"collected_at": "not-a-date", "label": "' + b"x" * 201 + b'"}'
        )
        (item,) = store.list("snapshot").items
        assert item.id == saved_id
        assert item.collected_at is None
        assert item.label is None

    def test_long_but_valid_headers_kept(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        label = "x" * 200
        saved_id = store.save(
            "snapshot",
            {"collected_at": "2026-11-04T15:15:00Z", "label": label},
        )
        (item,) = store.list("snapshot").items
        assert item.id == saved_id
        assert item.collected_at == "2026-11-04T15:15:00Z"
        assert item.label == label

    def test_unreadable_files_still_listed(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"n": 1})
        (store.module_dir / (saved_id + ".json")).write_bytes(b"not json{")
        (item,) = store.list("snapshot").items
        assert item.id == saved_id
        assert item.collected_at is None

    def test_other_kinds_excluded(self, tmp_path):
        store = self.fill(tmp_path, 2)
        store.save("other", {"n": 9})
        page = store.list("other")
        assert page.total == 1

    def test_bad_paging_rejected(self, tmp_path):
        store = self.fill(tmp_path, 1)
        with pytest.raises(ValueError):
            store.list("snapshot", offset=-1)
        with pytest.raises(ValueError):
            store.list("snapshot", limit=0)
        with pytest.raises(ValueError):
            store.list("snapshot", limit=101)
        with pytest.raises(ValueError):
            store.list("Nope")


class TestFifoAndShortReads:
    def test_list_fifo_does_not_hang(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        store.save("snapshot", {"n": 1})
        fifo_id = "snapshot_20261104T151500Z_0002"
        os.mkfifo(store.module_dir / (fifo_id + ".json"))
        done: dict = {}

        def target() -> None:
            done["page"] = store.list("snapshot")

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        (item,) = [
            entry for entry in done["page"].items if entry.id == fifo_id
        ]
        assert item.collected_at is None
        assert item.label is None

    def test_load_fifo_does_not_hang(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        store.save("snapshot", {"n": 1})
        fifo_id = "snapshot_20261104T151500Z_0002"
        os.mkfifo(store.module_dir / (fifo_id + ".json"))
        done: dict = {}

        def target() -> None:
            try:
                store.load(fifo_id)
            except StoreError as exc:
                done["error"] = exc

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert done["error"].code == "not_found"

    def test_short_reads_still_load(self, tmp_path, monkeypatch):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        payload = {
            "n": 1,
            "collected_at": "2026-11-04T15:15:00Z",
            "label": "reading",
            "pad": "x" * 5000,
        }
        saved_id = store.save("snapshot", payload)
        real_read = os.read
        monkeypatch.setattr(
            os, "read", lambda fd, n: real_read(fd, min(n, 3))
        )
        assert store.load(saved_id) == payload
        (item,) = store.list("snapshot").items
        assert item.collected_at == "2026-11-04T15:15:00Z"
        assert item.label == "reading"


class TestLoad:
    def test_missing_valid_id(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(StoreError) as excinfo:
            store.load("snapshot_20261104T151500Z_3fa1")
        assert excinfo.value.code == "not_found"
        assert "snapshot_20261104T151500Z_3fa1" in str(excinfo.value)

    @pytest.mark.parametrize(
        "bad_id",
        ["../evil", "snapshot_20261104T151500Z_ZZZZ", "nope", "", "a" * 500],
    )
    def test_invalid_ids_never_echoed(self, tmp_path, bad_id):
        store = make_store(tmp_path)
        with pytest.raises(StoreError) as excinfo:
            store.load(bad_id)
        assert excinfo.value.code == "not_found"
        assert excinfo.value.reason == "saved result not found"
        if bad_id:
            assert bad_id not in str(excinfo.value)

    def test_traversal_id_refused(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(StoreError) as excinfo:
            store.load("snapshot_20261104T151500Z_3fa1/../../x")
        assert excinfo.value.code == "not_found"

    def test_corrupt_file_is_unreadable(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"n": 1})
        (store.module_dir / (saved_id + ".json")).write_bytes(b"{oops")
        with pytest.raises(StoreError) as excinfo:
            store.load(saved_id)
        assert excinfo.value.code == "not_found"
        assert excinfo.value.reason == "saved result is unreadable"

    def test_non_object_file_is_unreadable(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        store.save("snapshot", {"n": 1})
        raw = store.module_dir / "snapshot_20261104T151500Z_0001.json"
        raw.write_bytes(b"[1,2]")
        with pytest.raises(StoreError) as excinfo:
            store.load("snapshot_20261104T151500Z_0001")
        assert excinfo.value.reason == "saved result is unreadable"

    def test_symlinked_file_refused(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"n": 1})
        target = store.module_dir / (saved_id + ".json")
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text('{"n": 2}')
        target.unlink()
        os.symlink(elsewhere, target)
        with pytest.raises(StoreError) as excinfo:
            store.load(saved_id)
        assert excinfo.value.reason == "saved result is unreadable"
        assert "elsewhere" not in str(excinfo.value)

    def test_saved_content_is_json_object(self, tmp_path):
        store = make_store(tmp_path, clock=clock_over(0), suffix=suffixes("0001"))
        saved_id = store.save("snapshot", {"a": [1, {"b": "c"}]})
        assert json.loads(
            (store.module_dir / (saved_id + ".json")).read_text()
        ) == {"a": [1, {"b": "c"}]}


def test_list_kind_is_exact_not_prefix(tmp_path):
    """Kinds in a prefix relation (snap / snap_full) list separately."""
    store = make_store(
        tmp_path, clock=clock_over(0, 1), suffix=suffixes("0001", "0002")
    )
    store.save("snap_full", {"n": 1})
    store.save("snap", {"n": 2})
    page = store.list("snap")
    assert page.total == 1
    assert [item.kind for item in page.items] == ["snap"]
    assert store.list("snap_full").total == 1
