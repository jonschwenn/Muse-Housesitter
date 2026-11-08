"""Build the read-only dashboard view (docs/DASHBOARD.md §4).

Pure data assembly: reads saved snapshots through the module's own
``get`` handler and the shared runner output policy, but never touches
sockets or writes anything. The HTTP layer lives in ``core/serve.py``.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from housesitter import __version__, sdk
from housesitter.core import envelope as _envelope
from housesitter.core import runner as _runner
from housesitter.core.context import ModuleContext
from housesitter.core.store import FileStore, StoreError

SCHEMA = "housesitter.dashboard/1"

# In v1 the dashboard has a single source: saved unifi_wifi snapshots,
# listed by kind and read back through the module's own ``get`` handler.
# Naming a module id here is fine; importing module internals is not
# (AGENTS.md rule 9). The registry supplies the module instance.
_DASHBOARD_SOURCES = {"unifi_wifi": ("snapshot", "get")}

# How many history entries the newest-first scan keeps full objects for,
# and how many history entries the cache holds overall.
_MAX_RECENT = 50
# The history cache must cover the whole store, or evicted files would be
# re-validated on every request. Entries for files no longer listed (pruned
# by retention) are dropped after each scan; this cap is only a backstop for
# ``retention: 0`` stores. A history entry is a few hundred bytes.
_MAX_HISTORY_CACHE = 20000

# Cache of per-file validation: ``(id, st_ino, st_mtime_ns, st_size)`` to
# ``("ok", history_entry)`` or ``("bad",)``. Saved files are never
# overwritten, so a stat key that matches means the content matched too
# and the file is validated at most once. Full snapshot objects are held
# only for the newest few files (latest and previous).
_cache_lock = threading.RLock()
_history_cache: dict = {}
_full_cache: dict = {}


class DashboardUnavailable(Exception):
    """The view cannot be built: the store or config failed. Served as 503
    with a fixed body, never with the underlying reason text."""


def clear_cache() -> None:
    """Drop the dashboard file cache (tests seed many stores in a row)."""
    with _cache_lock:
        _history_cache.clear()
        _full_cache.clear()


def _refuse_upstream(*args: Any, **kwargs: Any) -> Any:
    """A client factory that never opens a socket: the dashboard reads
    saved results only, so any upstream call is a wiring bug."""
    raise RuntimeError("the dashboard never contacts upstream systems")


def _stat_key(path: Any, store_id: str) -> tuple | None:
    """The cache key for one saved file, or None when it cannot be read.

    Uses lstat with no following, so a symlink planted in the data dir
    never resolves anywhere. Only the stat fields join the key: the id
    itself is store-validated, never logged or echoed.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (store_id, st.st_ino, st.st_mtime_ns, st.st_size)


def _prune_history_cache() -> None:
    while len(_history_cache) > _MAX_HISTORY_CACHE:
        _history_cache.pop(next(iter(_history_cache)))


def _load_validated(
    *,
    registry: Any,
    config: Any,
    module_id: str,
    handler_name: str,
    expected_schema: str,
    store_id: str,
    data_store: FileStore,
    now: Any,
) -> tuple | None:
    """Validate one saved file, returning ``(key, history, full)``.

    Returns None when the file is unreadable (logged once, with the
    store-validated id only, then cached as bad). Raises
    :class:`DashboardUnavailable` when the store itself fails: an unsafe
    data dir must answer 503, not read as empty.
    """
    # Check the cache by stat key before reading anything: a hit must
    # not re-read or re-parse the file, or every request would parse the
    # whole store. The identifier policy joins the key because config is
    # reloaded per request: switching to ``identifiers: none`` must never
    # serve entries projected under ``full``.
    stat_key = _stat_key(
        data_store.module_dir / (store_id + ".json"), store_id
    )
    if stat_key is None:
        _note_skipped(store_id)
        return None
    key = stat_key + (config.identifiers,)
    with _cache_lock:
        hit = _history_cache.get(key)
        if hit is not None and hit[0] == "bad":
            return None
        if hit is not None:
            full = _full_cache.get(key)
            return (key, hit[1], full)
    try:
        raw = data_store.load(store_id)
    except StoreError as exc:
        if exc.code == "config_invalid":
            raise DashboardUnavailable from None
        _note_bad(store_id, data_store, key)
        return None
    if not isinstance(raw, dict) or raw.get("schema") != expected_schema:
        _note_bad(store_id, data_store, key)
        return None
    try:
        registered = registry.get_module(module_id)
        instance = registered.cls()
        get_registered = registry.get_command(
            f"housesitter.{module_id}.{handler_name}"
        )
        ctx = ModuleContext(
            instance,
            dict(config.module(module_id).settings),
            secrets_dir=config.secrets_dir,
            deadline_monotonic=time.monotonic() + 9,
            now=now,
            store=data_store,
            client_factory=_refuse_upstream,
        )
        handler = getattr(instance, handler_name, None)
        result = handler(ctx, {"id": store_id})
        if not isinstance(result, sdk.Result):
            raise ValueError("get handler did not return a Result")
        prepared = _runner.prepare_output(
            result,
            get_registered.command,
            identifiers=config.identifiers,
            extra_warnings=ctx.warnings,
            default_now=ctx.now(),
        )
        if config.identifiers == "none":
            _null_identifiers(prepared.data, get_registered.command.fields)
        history = {
            "id": store_id,
            "collected_at": prepared.collected_at,
            "label": prepared.label,
            "ap_count": prepared.data["summary"]["ap_count"],
            "client_count": prepared.data["summary"]["client_count"],
            "clients_by_band": prepared.data["summary"]["clients_by_band"],
        }
        full = {
            "id": store_id,
            "collected_at": prepared.collected_at,
            "label": prepared.label,
            "source": prepared.source,
            "warnings": prepared.warnings,
            "summary": prepared.data["summary"],
            "aps": prepared.data["aps"],
            "clients": prepared.data["clients"],
        }
    except DashboardUnavailable:
        raise
    except Exception:
        with _cache_lock:
            _history_cache[key] = ("bad",)
            _prune_history_cache()
        _note_bad(store_id, data_store, key)
        return None
    with _cache_lock:
        _history_cache[key] = ("ok", history)
        _prune_history_cache()
    return (key, history, full)


def _null_identifiers(data: Any, fields: Any) -> None:
    """Keep hidden identifier keys as ``null`` (D24).

    With ``identifiers: "none"`` the shared output policy drops every
    declared identifier value. ``invoke`` output leaves the keys out, but
    the dashboard contract pins exact key sets, so the view puts each
    dropped key back with ``null``. No value ever returns: only keys that
    are absent are filled, and only with ``None``.
    """
    for item in fields:
        if not item.identifier or not item.path.startswith("data."):
            continue
        parts = item.path.split(".")[1:]
        nodes = [data]
        for part in parts[:-1]:
            step = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                if part.endswith("[]"):
                    value = node.get(part[:-2])
                    if isinstance(value, list):
                        step.extend(value)
                else:
                    step.append(node.get(part))
            nodes = step
        leaf = parts[-1]
        for node in nodes:
            if isinstance(node, dict) and leaf not in node:
                node[leaf] = None


def _note_bad(store_id: str, data_store: FileStore, key: Any) -> None:
    """Cache a miss as bad (when keyed) and log one line with the id only."""
    if key is not None:
        with _cache_lock:
            _history_cache[key] = ("bad",)
            _prune_history_cache()
    _note_skipped(store_id)


def _note_skipped(store_id: str) -> None:
    """One stderr line for an unreadable saved file: the store-validated
    id only, never contents (which may hold secrets or hostile text)."""
    try:
        sys.stderr.write(
            f"housesitter: dashboard: skipping unreadable snapshot {store_id}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _list_all(data_store: FileStore, kind: str) -> tuple[list, int]:
    """Every snapshot id, newest first, plus the listing total.

    Paging through ``list`` keeps each call within its 1..100 limit.
    Raises :class:`DashboardUnavailable` when the store itself fails.
    """
    ids: list[str] = []
    offset = 0
    total = 0
    while True:
        try:
            page = data_store.list(kind, offset=offset, limit=100)
        except StoreError as exc:
            if exc.code == "config_invalid":
                raise DashboardUnavailable from None
            raise
        total = page.total
        if not page.items:
            break
        ids.extend(item.id for item in page.items)
        offset += len(page.items)
        if offset >= page.total:
            break
    return ids, total


def _format_now(now: Any) -> str:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_view(
    *,
    registry: Any,
    config: Any,
    env: Mapping[str, str] | None = None,
    now: Any = None,
) -> dict:
    """Assemble the ``housesitter.dashboard/1`` view from saved results.

    Reads only: listing and loading never create directories or change
    modes, and the ``get`` handler runs with a client factory that
    refuses any upstream call. Raises :class:`DashboardUnavailable`
    when the store or config fails, so the server can answer 503.
    """
    generated_at = _format_now(now)
    raw_label = config.dashboard.house_label
    if raw_label is None:
        house_label = None
    else:
        house_label = _envelope.sanitize_label(raw_label) or None
    modules: list[dict] = []
    unifi: dict | None = None
    for module_id, (kind, handler_name) in _DASHBOARD_SOURCES.items():
        module_cfg = config.module(module_id)
        if not module_cfg.enabled:
            continue
        try:
            registered = registry.get_module(module_id)
        except KeyError:
            continue
        data_store = FileStore(
            module_id, env=env, retention=module_cfg.retention
        )
        ids, listed_total = _list_all(data_store, kind)
        snapshot_cmd = registry.get_command(
            f"housesitter.{module_id}.snapshot"
        )
        expected_schema = (
            f"housesitter.{module_id}."
            f"{snapshot_cmd.command.name}/{snapshot_cmd.command.schema_version}"
        )
        valid: list[tuple] = []
        held: list[int] = []
        for store_id in ids:
            loaded = _load_validated(
                registry=registry,
                config=config,
                module_id=module_id,
                handler_name=handler_name,
                expected_schema=expected_schema,
                store_id=store_id,
                data_store=data_store,
                now=now,
            )
            if loaded is not None:
                valid.append(loaded)
                # Hold full objects only for the two newest valid files
                # by (collected_at, id), the order used below; the rest
                # need only their history entry. Listing order is save
                # order, which can differ from collected_at order.
                if loaded[2] is not None:
                    held.append(len(valid) - 1)
                if len(held) > 2:
                    oldest = min(
                        held,
                        key=lambda i: (valid[i][1]["collected_at"], valid[i][1]["id"]),
                    )
                    held.remove(oldest)
                    valid[oldest] = (valid[oldest][0], valid[oldest][1], None)
        # The contract needs strictly descending unique times, but two
        # saves within the same second share a collected_at. Keep the
        # newest file for each stamp (the listing is newest first) and
        # skip the older twin: both files stay on disk, only one shows.
        # Counting happens after this dedupe, so total and recent agree.
        seen: set[str] = set()
        unique: list[tuple] = []
        for key, history, full in valid:
            stamp = history["collected_at"]
            if stamp in seen:
                continue
            seen.add(stamp)
            unique.append((key, history, full))
        unique.sort(
            key=lambda entry: (entry[1]["collected_at"], entry[1]["id"]),
            reverse=True,
        )
        # Resolve full objects for the two newest. A file can vanish or
        # turn unreadable between the scan and here (a concurrent save's
        # retention prune): drop it and move on, so latest, previous,
        # recent, and total always come from the same resolved set.
        full_by_id: dict = {}
        index = 0
        while index < len(unique) and len(full_by_id) < 2:
            key, history, full = unique[index]
            if full is None:
                with _cache_lock:
                    _history_cache.pop(key, None)
                    _full_cache.pop(key, None)
                reloaded = _load_validated(
                    registry=registry,
                    config=config,
                    module_id=module_id,
                    handler_name=handler_name,
                    expected_schema=expected_schema,
                    store_id=history["id"],
                    data_store=data_store,
                    now=now,
                )
                full = reloaded[2] if reloaded is not None else None
                if reloaded is not None:
                    key = reloaded[0]
            if full is None:
                del unique[index]
                continue
            full_by_id[history["id"]] = full
            with _cache_lock:
                _full_cache[key] = full
            index += 1
        total = len(unique)
        recent = [history for _, history, _ in unique[:_MAX_RECENT]]
        wanted = set(full_by_id)
        listed = set(ids)
        with _cache_lock:
            for key in [key for key in _full_cache if key[0] not in wanted]:
                del _full_cache[key]
            for key in [key for key in _history_cache if key[0] not in listed]:
                del _history_cache[key]
        if total == 0 and listed_total > 0:
            entry = {
                "id": module_id,
                "title": registered.cls.title,
                "version": registered.cls.version,
                "status": "error",
                "last_success_at": None,
                "last_attempt_at": None,
                "last_error": {
                    "code": "upstream_unexpected",
                    "message": "Saved snapshots could not be read.",
                },
            }
            modules.append(entry)
            unifi = None
        elif total == 0:
            modules.append(
                {
                    "id": module_id,
                    "title": registered.cls.title,
                    "version": registered.cls.version,
                    "status": "never",
                    "last_success_at": None,
                    "last_attempt_at": None,
                    "last_error": None,
                }
            )
            unifi = None
        else:
            newest = recent[0]["collected_at"]
            modules.append(
                {
                    "id": module_id,
                    "title": registered.cls.title,
                    "version": registered.cls.version,
                    "status": "ok",
                    "last_success_at": newest,
                    "last_attempt_at": newest,
                    "last_error": None,
                }
            )
            latest = full_by_id.get(recent[0]["id"])
            previous = (
                full_by_id.get(recent[1]["id"])
                if len(recent) >= 2
                else None
            )
            unifi = {
                "snapshots": {"total": total, "recent": recent},
                "latest": latest,
                "previous": previous,
            }
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "housesitter_version": __version__,
        "house_label": house_label,
        "modules": modules,
        "muse_note": None,
        "unifi_wifi": unifi,
    }
