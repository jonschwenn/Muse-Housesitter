"""Pure normalisation for the unifi_wifi module (no I/O, no Context).

Each function maps invented-or-real UniFi controller records (plain
dicts) to the documented output shape in docs/modules/unifi_wifi.md.
Only allow-listed fields are ever copied; anything else (including
``x_*`` secret-bearing keys) is ignored.

U1 marks places where the upstream shape is community-documented and
must be verified on a live controller (#16).
"""

from __future__ import annotations

import ipaddress
import math
import re
from typing import Any

_MAC_RE = re.compile(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}\Z")
_CHANNEL_SETTING_RE = re.compile(r"[A-Za-z0-9]{1,16}\Z")
# What collection can store: the strict word above, or the decimal string
# of a numeric upstream channel (which may be negative).
_SAVED_CHANNEL_SETTING_RE = re.compile(r"(-?[0-9]{1,16}|[A-Za-z0-9]{1,16})\Z")
_WIFI_RAW_RE = re.compile(r"[A-Za-z0-9._-]{1,16}\Z")

_BANDS = ("2.4GHz", "5GHz", "6GHz", "unknown")

_WIFI_STANDARD = {
    "a": "a",
    "b": "b",
    "g": "g",
    "n": "n",
    "ac": "ac",
    "ax": "ax",
    "be": "be",
    "na": "n",
    "ng": "n",
}

_TX_POWER_MODES = frozenset({"auto", "high", "medium", "low", "custom"})
_UPLINK_TYPES = frozenset({"wire", "wireless"})
_WIFI_STANDARD_VALUES = frozenset(_WIFI_STANDARD.values())

_SITE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SOURCE_KIND = "unifi"
_SOURCE_FLAVORS = frozenset({"unifi_os", "classic"})

#: Fixed reason for a saved snapshot that fails re-validation.
DAMAGED_SNAPSHOT = "That saved snapshot is damaged."

_COMPACT_DROP = frozenset({
    "noise_dbm", "idle_s", "vendor", "wifi_standard_raw", "guest", "ip",
    "tx_retry_pct_recent",
})


_MAX_MAGNITUDE = 2**53
_MAX_NUMERIC_LEN = 32


def _to_number(value: Any) -> Any:
    """An int/float (never bool, always finite) or numeric string, else None.

    Values past 2**53 (or numeric strings longer than 32 characters)
    are untrusted-upstream noise the radio math cannot use exactly
    (huge ints even raise OverflowError on division), so they are None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > _MAX_NUMERIC_LEN:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    if isinstance(num, float) and not math.isfinite(num):
        return None
    if abs(num) > _MAX_MAGNITUDE:
        return None
    return num


def _to_int(value: Any) -> Any:
    """An integer value (integral floats and numeric strings count), else None."""
    num = _to_number(value)
    if num is None:
        return None
    if isinstance(num, float):
        if not num.is_integer():
            return None
        return int(num)
    return num


def _pct(value: Any) -> Any:
    """A 0..100 percent as an int (integral values only), else None."""
    num = _to_int(value)
    if num is None or num < 0 or num > 100:
        return None
    return num


def _pct_number(value: Any) -> Any:
    """A 0..100 percent that may be fractional (saved derived figures)."""
    num = _to_number(value)
    if num is None or num < 0 or num > 100:
        return None
    return num


def _strict_word(value: Any, pattern: Any) -> Any:
    """A strictly-validated non-untrusted string, else None."""
    if isinstance(value, str) and pattern.fullmatch(value) is not None:
        return value
    return None


def _normalise_mac(value: Any) -> Any:
    """A lowercase ``xx:xx:xx:xx:xx:xx`` MAC, else None."""
    if isinstance(value, str) and _MAC_RE.fullmatch(value) is not None:
        return value.lower()
    return None


def _normalise_ip(value: Any) -> Any:
    """A parseable IP address string without an IPv6 scope, else None.

    ``ipaddress`` accepts ``2001:db8::1%<any text>`` on Python 3.9+, and
    ``ip`` is an identifier field the runner does not sanitise, so a
    scoped address is refused rather than passed through.
    """
    if not isinstance(value, str) or not value or "%" in value:
        return None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def _band(radio: Any) -> str:
    if radio == "ng":
        return "2.4GHz"
    if radio == "na":
        return "5GHz"
    if radio == "6e":
        return "6GHz"
    return "unknown"


def _cumulative_retry(packets: Any, retries: Any) -> Any:
    """``retries / (packets + retries) * 100``, unrounded, else None."""
    pkts = _to_number(packets)
    retr = _to_number(retries)
    if pkts is None or retr is None:
        return None
    if pkts < 0 or retr < 0:
        return None
    if pkts == 0 and retr == 0:
        return None
    return retr / (pkts + retr) * 100


def _radio_record(table: Any, stats_by_name: Any) -> Any:
    """One normalised radio from a radio_table entry plus its stats."""
    if not isinstance(table, dict):
        return None
    name = table.get("name")
    stats = stats_by_name.get(name) if isinstance(name, str) else None
    if not isinstance(stats, dict):
        stats = {}
    radio = table.get("radio")
    if not isinstance(radio, str):
        radio = stats.get("radio")
    band = _band(radio)
    stats_channel = _to_int(stats.get("channel"))
    table_channel = _to_int(table.get("channel"))
    if stats_channel is not None:
        channel = stats_channel
    elif table_channel is not None:
        channel = table_channel
    else:
        channel = None
    raw_setting = table.get("channel")
    if _to_int(raw_setting) is not None:
        channel_setting = str(_to_int(raw_setting))
    elif isinstance(raw_setting, str) and _CHANNEL_SETTING_RE.fullmatch(
        raw_setting
    ) is not None:
        channel_setting = raw_setting
    else:
        channel_setting = None
    # U1: ht is community-documented as the channel width in MHz.
    width = _to_int(table.get("ht"))
    return {
        "band": band,
        "channel": channel,
        "channel_setting": channel_setting,
        "width_mhz": width,
        "tx_power_dbm": _to_int(stats.get("tx_power")),
        "tx_power_mode": (
            table.get("tx_power_mode")
            if table.get("tx_power_mode") in _TX_POWER_MODES
            else None
        ),
        "channel_utilization_pct": _pct(stats.get("cu_total")),
        "self_rx_pct": _pct(stats.get("cu_self_rx")),
        "self_tx_pct": _pct(stats.get("cu_self_tx")),
        "client_count": _to_int(stats.get("num_sta")),
        "satisfaction_pct": _pct(stats.get("satisfaction")),
        "tx_retry_pct_cumulative": _cumulative_retry(
            stats.get("tx_packets"), stats.get("tx_retries")
        ),
    }


def normalise_aps(devices: Any) -> tuple:
    """Normalise ``stat/device`` records to ``(aps, warnings)``.

    Only ``type == "uap"`` records with a valid MAC are kept. Records
    are skipped (and counted in a fixed-text warning) when they have no
    valid MAC; duplicate MACs keep the first record (counted).
    ``online`` is literally ``state == 1``: a missing state
    means offline.
    """
    warnings: list[str] = []
    aps: list[dict] = []
    skipped = 0
    duplicates = 0
    seen: set[str] = set()
    records = devices if isinstance(devices, list) else []
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        if record.get("type") != "uap":
            continue
        ap_id = _normalise_mac(record.get("mac"))
        if ap_id is None:
            skipped += 1
            continue
        if ap_id in seen:
            duplicates += 1
            continue
        seen.add(ap_id)
        stats_by_name: dict[str, Any] = {}
        stats_list = record.get("radio_table_stats")
        if isinstance(stats_list, list):
            for entry in stats_list:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and entry.get("name") not in stats_by_name
                ):
                    stats_by_name[entry["name"]] = entry
        radios = []
        table = record.get("radio_table")
        if isinstance(table, list):
            for item in table:
                radio_out = _radio_record(item, stats_by_name)
                if radio_out is not None:
                    radios.append(radio_out)
        uplink = record.get("uplink")
        if not isinstance(uplink, dict):
            uplink = {}
        uplink_type = uplink.get("type")
        if uplink_type not in _UPLINK_TYPES:
            uplink_type = None
        name = record.get("name")
        if not isinstance(name, str) or not name:
            name = None
        aps.append({
            "id": ap_id,
            "name": name,
            "model": _strict_word(record.get("model"), _MODEL_RE),
            "firmware": _strict_word(record.get("version"), _MODEL_RE),
            "online": _to_int(record.get("state")) == 1,
            "uptime_s": _to_int(record.get("uptime")),
            "client_count": _to_int(record.get("num_sta")),
            "satisfaction_pct": _pct(record.get("satisfaction")),
            "uplink_type": uplink_type,
            "uplink_speed_mbps": _to_int(uplink.get("speed")),
            "radios": radios,
        })
    if skipped == 1:
        warnings.append(
            "1 AP record could not be read or had no valid MAC address "
            "and was left out"
        )
    elif skipped > 1:
        warnings.append(
            f"{skipped} AP records could not be read or had no valid "
            "MAC address and were left out"
        )
    if duplicates == 1:
        warnings.append(
            "1 duplicate AP MAC address was left out"
        )
    elif duplicates > 1:
        warnings.append(
            f"{duplicates} duplicate AP MAC addresses were left out"
        )
    aps.sort(key=lambda item: (item["name"] is None, item["name"] or "",
                               item["id"]))
    return aps, warnings


def _client_name(record: Any) -> Any:
    name = record.get("name")
    if isinstance(name, str) and name:
        return name
    hostname = record.get("hostname")
    if isinstance(hostname, str) and hostname:
        return hostname
    return None


def _client_record(record: Any, aps_by_id: Any) -> Any:
    """One normalised client, or None when the record is excluded."""
    if not isinstance(record, dict):
        return None
    # U1: missing is_wired counts as wireless (wired clients carry
    # is_wired=true; the flag's absence on wireless records is the
    # community-observed shape).
    if record.get("is_wired") is True:
        return None
    client_id = _normalise_mac(record.get("mac"))
    if client_id is None:
        return None
    ap_mac = _normalise_mac(record.get("ap_mac"))
    ap_id = ap_mac if ap_mac in aps_by_id else None
    ap_name = aps_by_id[ap_id]["name"] if ap_id is not None else None
    radio = record.get("radio")
    radio_proto = record.get("radio_proto")
    if isinstance(radio_proto, str) and radio_proto in _WIFI_STANDARD:
        wifi_standard: Any = _WIFI_STANDARD[radio_proto]
    else:
        wifi_standard = None
    tx_rate = _to_number(record.get("tx_rate"))
    rx_rate = _to_number(record.get("rx_rate"))
    raw_proto: Any
    if isinstance(radio_proto, str) and _WIFI_RAW_RE.fullmatch(
        radio_proto
    ) is not None:
        raw_proto = radio_proto
    else:
        raw_proto = None
    guest = record.get("is_guest")
    if not isinstance(guest, bool):
        guest = None
    return {
        "id": client_id,
        "ip": _normalise_ip(record.get("ip")),
        "name": _client_name(record),
        "vendor": (
            record.get("oui") if isinstance(record.get("oui"), str) else None
        ),
        "private_mac": bool(int(client_id.split(":")[0], 16) & 0x02),
        "ssid": (
            record.get("essid")
            if isinstance(record.get("essid"), str)
            else None
        ),
        "ap_id": ap_id,
        "ap_name": ap_name,
        "band": _band(radio),
        "channel": _to_int(record.get("channel")),
        "wifi_standard": wifi_standard,
        "wifi_standard_raw": raw_proto,
        "signal_dbm": _to_int(record.get("signal")),
        "noise_dbm": _to_int(record.get("noise")),
        "tx_rate_mbps": tx_rate / 1000 if tx_rate is not None else None,
        "rx_rate_mbps": rx_rate / 1000 if rx_rate is not None else None,
        "satisfaction_pct": _pct(record.get("satisfaction")),
        "tx_retry_pct_cumulative": _cumulative_retry(
            record.get("tx_packets"), record.get("tx_retries")
        ),
        # U1: wifi_tx_retries_percentage is believed to cover a recent
        # window, not the association lifetime.
        "tx_retry_pct_recent": _pct(
            record.get("wifi_tx_retries_percentage")
        ),
        "connected_s": _to_int(record.get("uptime")),
        "idle_s": _to_int(record.get("idletime")),
        "guest": guest,
    }


def normalise_clients(stations: Any, aps: Any) -> tuple:
    """Normalise ``stat/sta`` records to ``(clients, warnings)``.

    Wired records are excluded. Records without a valid MAC are
    skipped (counted); duplicate MACs keep the first record (counted).
    Clients sort by AP name, then name, then id, with nulls last.
    """
    warnings: list[str] = []
    aps_by_id = {item["id"]: item for item in aps}
    clients: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    duplicates = 0
    records = stations if isinstance(stations, list) else []
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        if record.get("is_wired") is True:
            continue
        client_id = _normalise_mac(record.get("mac"))
        if client_id is None:
            skipped += 1
            continue
        if client_id in seen:
            duplicates += 1
            continue
        seen.add(client_id)
        clients.append(_client_record(record, aps_by_id))
    if skipped == 1:
        warnings.append(
            "1 client record could not be read or had no valid MAC "
            "address and was left out"
        )
    elif skipped > 1:
        warnings.append(
            f"{skipped} client records could not be read or had no "
            "valid MAC address and were left out"
        )
    if duplicates == 1:
        warnings.append(
            "1 duplicate client MAC address was left out"
        )
    elif duplicates > 1:
        warnings.append(
            f"{duplicates} duplicate client MAC addresses were left out"
        )
    clients.sort(key=lambda item: (
        item["ap_name"] is None, item["ap_name"] or "",
        item["name"] is None, item["name"] or "", item["id"]))
    return clients, warnings


def compact_client(client: Any) -> Any:
    """The smaller per-client form (drops seven fields)."""
    return {
        key: value
        for key, value in client.items()
        if key not in _COMPACT_DROP
    }


def build_summary(aps: Any, clients: Any) -> Any:
    """Counts only: bands in fixed order, APs by id, SSIDs null-last."""
    band_counts = {band: 0 for band in _BANDS}
    for client in clients:
        band_counts[client["band"]] = band_counts.get(
            client["band"], 0) + 1
    by_band = [
        {"band": band, "count": band_counts.get(band, 0)} for band in _BANDS
    ]
    ap_groups: dict[str, list] = {}
    for client in clients:
        if client["ap_id"] is None:
            continue
        ap_groups.setdefault(client["ap_id"], []).append(client)
    by_ap = [
        {"ap_id": ap_id, "ap_name": items[0]["ap_name"],
         "count": len(items)}
        for ap_id, items in sorted(ap_groups.items())
    ]
    ssid_groups: dict[Any, int] = {}
    null_count = 0
    for client in clients:
        if client["ssid"] is None:
            null_count += 1
        else:
            ssid_groups[client["ssid"]] = ssid_groups.get(
                client["ssid"], 0) + 1
    by_ssid = [
        {"ssid": ssid, "count": count}
        for ssid, count in sorted(ssid_groups.items())
    ]
    if null_count:
        by_ssid.append({"ssid": None, "count": null_count})
    return {
        "ap_count": len(aps),
        "ap_online_count": sum(1 for item in aps if item["online"]),
        "client_count": len(clients),
        "clients_by_band": by_band,
        "clients_by_ap": by_ap,
        "clients_by_ssid": by_ssid,
    }


def _revalidate_band(value: Any) -> str:
    """A known band, else ``"unknown"`` (never trust the saved text)."""
    if isinstance(value, str) and value in _BANDS:
        return value
    return "unknown"


def _revalidate_radio(item: Any) -> Any:
    """One saved radio, re-checked field by field, or None when unreadable."""
    if not isinstance(item, dict):
        return None
    tx_power_mode = item.get("tx_power_mode")
    if not isinstance(tx_power_mode, str) or tx_power_mode not in _TX_POWER_MODES:
        tx_power_mode = None
    return {
        "band": _revalidate_band(item.get("band")),
        "channel": _to_int(item.get("channel")),
        "channel_setting": _strict_word(
            item.get("channel_setting"), _SAVED_CHANNEL_SETTING_RE
        ),
        "width_mhz": _to_int(item.get("width_mhz")),
        "tx_power_dbm": _to_int(item.get("tx_power_dbm")),
        "tx_power_mode": tx_power_mode,
        "channel_utilization_pct": _pct(item.get("channel_utilization_pct")),
        "self_rx_pct": _pct(item.get("self_rx_pct")),
        "self_tx_pct": _pct(item.get("self_tx_pct")),
        "client_count": _to_int(item.get("client_count")),
        "satisfaction_pct": _pct(item.get("satisfaction_pct")),
        "tx_retry_pct_cumulative": _pct_number(
            item.get("tx_retry_pct_cumulative")
        ),
    }


def _revalidate_ap(item: Any) -> Any:
    """One saved AP, re-checked with the collection rules, or None."""
    if not isinstance(item, dict):
        return None
    ap_id = _normalise_mac(item.get("id"))
    if ap_id is None:
        return None
    name = item.get("name")
    uplink_type = item.get("uplink_type")
    if not isinstance(uplink_type, str) or uplink_type not in _UPLINK_TYPES:
        uplink_type = None
    radios: list[dict] = []
    saved_radios = item.get("radios")
    if isinstance(saved_radios, list):
        for entry in saved_radios:
            radio = _revalidate_radio(entry)
            if radio is not None:
                radios.append(radio)
    return {
        "id": ap_id,
        "name": name if isinstance(name, str) else None,
        "model": _strict_word(item.get("model"), _MODEL_RE),
        "firmware": _strict_word(item.get("firmware"), _MODEL_RE),
        "online": item.get("online") is True,
        "uptime_s": _to_int(item.get("uptime_s")),
        "client_count": _to_int(item.get("client_count")),
        "satisfaction_pct": _pct(item.get("satisfaction_pct")),
        "uplink_type": uplink_type,
        "uplink_speed_mbps": _to_int(item.get("uplink_speed_mbps")),
        "radios": radios,
    }


def _revalidate_client(item: Any, aps_by_id: Any) -> Any:
    """One saved client, re-checked with the collection rules, or None."""
    if not isinstance(item, dict):
        return None
    client_id = _normalise_mac(item.get("id"))
    if client_id is None:
        return None
    ap_id = _normalise_mac(item.get("ap_id"))
    if ap_id not in aps_by_id:
        ap_id = None
    ap_name = aps_by_id[ap_id]["name"] if ap_id is not None else None
    wifi_standard = item.get("wifi_standard")
    if (
        not isinstance(wifi_standard, str)
        or wifi_standard not in _WIFI_STANDARD_VALUES
    ):
        wifi_standard = None
    guest = item.get("guest")
    if not isinstance(guest, bool):
        guest = None
    name = item.get("name")
    vendor = item.get("vendor")
    ssid = item.get("ssid")
    return {
        "id": client_id,
        "ip": _normalise_ip(item.get("ip")),
        "name": name if isinstance(name, str) else None,
        "vendor": vendor if isinstance(vendor, str) else None,
        "private_mac": bool(int(client_id.split(":")[0], 16) & 0x02),
        "ssid": ssid if isinstance(ssid, str) else None,
        "ap_id": ap_id,
        "ap_name": ap_name,
        "band": _revalidate_band(item.get("band")),
        "channel": _to_int(item.get("channel")),
        "wifi_standard": wifi_standard,
        "wifi_standard_raw": _strict_word(
            item.get("wifi_standard_raw"), _WIFI_RAW_RE
        ),
        "signal_dbm": _to_int(item.get("signal_dbm")),
        "noise_dbm": _to_int(item.get("noise_dbm")),
        "tx_rate_mbps": _to_number(item.get("tx_rate_mbps")),
        "rx_rate_mbps": _to_number(item.get("rx_rate_mbps")),
        "satisfaction_pct": _pct(item.get("satisfaction_pct")),
        "tx_retry_pct_cumulative": _pct_number(
            item.get("tx_retry_pct_cumulative")
        ),
        "tx_retry_pct_recent": _pct(item.get("tx_retry_pct_recent")),
        "connected_s": _to_int(item.get("connected_s")),
        "idle_s": _to_int(item.get("idle_s")),
        "guest": guest,
    }


def saved_ids_intact(aps: Any, clients: Any) -> bool:
    """Every saved AP and client is a dict with a valid MAC id.

    A section page slices the saved lists by offset, so any unreadable
    or id-less record means the offsets cannot be trusted.
    """
    for records in (aps, clients):
        if not isinstance(records, list):
            return False
        for item in records:
            if not isinstance(item, dict):
                return False
            if _normalise_mac(item.get("id")) is None:
                return False
    return True


def revalidate_aps(aps: Any) -> tuple:
    """Re-check saved APs to ``(aps, skipped)``; bad records are dropped.

    ``get`` checks :func:`saved_ids_intact` first and fails a damaged
    file, so in practice nothing is dropped here; the drop is a backstop.
    """
    warnings: list[str] = []
    clean: list[dict] = []
    skipped = 0
    records = aps if isinstance(aps, list) else []
    for item in records:
        ap = _revalidate_ap(item)
        if ap is None:
            skipped += 1
        else:
            clean.append(ap)
    if skipped == 1:
        warnings.append(
            "1 AP record could not be read or had no valid MAC address "
            "and was left out"
        )
    elif skipped > 1:
        warnings.append(
            f"{skipped} AP records could not be read or had no valid "
            "MAC address and were left out"
        )
    return clean, warnings


def revalidate_clients(clients: Any, aps: Any) -> tuple:
    """Re-check saved clients to ``(clients, skipped)``; bad ones dropped.

    ``aps`` are the already re-validated APs, so the join matches
    collection. As for APs, ``get`` fails damaged files before this runs.
    """
    warnings: list[str] = []
    aps_by_id = {item["id"]: item for item in aps}
    clean: list[dict] = []
    skipped = 0
    records = clients if isinstance(clients, list) else []
    for item in records:
        client = _revalidate_client(item, aps_by_id)
        if client is None:
            skipped += 1
        else:
            clean.append(client)
    if skipped == 1:
        warnings.append(
            "1 client record could not be read or had no valid MAC "
            "address and was left out"
        )
    elif skipped > 1:
        warnings.append(
            f"{skipped} client records could not be read or had no "
            "valid MAC address and were left out"
        )
    return clean, warnings


def revalidate_source(source: Any) -> Any:
    """Re-check a saved source block, or raise ValueError when damaged."""
    if not isinstance(source, dict):
        raise ValueError(DAMAGED_SNAPSHOT)
    if source.get("kind") != _SOURCE_KIND:
        raise ValueError(DAMAGED_SNAPSHOT)
    flavor = source.get("flavor")
    if not isinstance(flavor, str) or flavor not in _SOURCE_FLAVORS:
        raise ValueError(DAMAGED_SNAPSHOT)
    site = source.get("site")
    if not isinstance(site, str) or _SITE_RE.fullmatch(site) is None:
        raise ValueError(DAMAGED_SNAPSHOT)
    return {
        "kind": _SOURCE_KIND,
        "flavor": flavor,
        "controller_version": _strict_word(
            source.get("controller_version"), _MODEL_RE
        ),
        "site": site,
    }
