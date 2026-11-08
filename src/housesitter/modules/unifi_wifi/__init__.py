"""UniFi WiFi radio metrics (read-only).

Gathers access-point radio conditions and wireless client readings
from a local UniFi Network controller and hands them to Muse.
It never changes anything upstream: every allow-listed path is a
read (plus login/logout, which the API needs).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from housesitter import sdk
from housesitter.modules.unifi_wifi import normalise

_SITE_PATTERN = r"[A-Za-z0-9_-]{1,64}"
# Port 1..65535 with no leading zeros (a bare host has no colon, so an
# out-of-range port can never hide inside the host part; bracketed IPv6
# literals keep working).
_PORT_PATTERN = (
    r"(6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|"
    r"[1-5][0-9]{4}|[1-9][0-9]{0,3})"
)
_BASE_URL_PATTERN = (
    r"https://([^/\s?#@:\[]+|\[[^\]/\s?#@]+\])(:" + _PORT_PATTERN + r")?/?"
)

_SITE_PREFIX_OS = "/proxy/network/api/s/%s"
_SITE_PREFIX_CLASSIC = "/api/s/%s"

_LOGIN_OS = "/api/auth/login"
_LOGIN_CLASSIC = "/api/login"
_LOGOUT_OS = "/api/auth/logout"
_LOGOUT_CLASSIC = "/api/logout"

_UNIFI_OS = "unifi_os"
_CLASSIC = "classic"

_SNAPSHOT_TIMEOUT_MS = 60_000
_GET_TIMEOUT_MS = 10_000

_SNAPSHOT_DESCRIPTION = (
    "Read WiFi access points, radio conditions, and wireless clients "
    "from the UniFi controller: channels, widths, signals, rates, and "
    "retries. Read-only. Logs in, reads, logs out, and saves the full "
    "snapshot locally. Takes up to a minute."
)
_GET_DESCRIPTION = (
    "Return a saved UniFi WiFi snapshot, or one page of its AP or "
    "client list. Read-only. Uses the local store, no network."
)


def _fields() -> tuple:
    f = sdk.Field
    return (
        f("data.summary", "object", nullable=False),
        f("data.summary.ap_count", "integer", nullable=False),
        f("data.summary.ap_online_count", "integer", nullable=False),
        f("data.summary.client_count", "integer", nullable=False),
        f("data.summary.clients_by_band", "array", nullable=False),
        f("data.summary.clients_by_band[].band", "string",
          nullable=False),
        f("data.summary.clients_by_band[].count", "integer",
          nullable=False),
        f("data.summary.clients_by_ap", "array", nullable=False),
        f("data.summary.clients_by_ap[].ap_id", "string",
          identifier=True),
        f("data.summary.clients_by_ap[].ap_name", "string",
          untrusted=True),
        f("data.summary.clients_by_ap[].count", "integer",
          nullable=False),
        f("data.summary.clients_by_ssid", "array", nullable=False),
        f("data.summary.clients_by_ssid[].ssid", "string",
          untrusted=True),
        f("data.summary.clients_by_ssid[].count", "integer",
          nullable=False),
        f("data.aps", "array", nullable=False),
        f("data.aps[].id", "string", nullable=False, identifier=True),
        f("data.aps[].name", "string", untrusted=True),
        f("data.aps[].model", "string"),
        f("data.aps[].firmware", "string"),
        f("data.aps[].online", "boolean", nullable=False),
        f("data.aps[].uptime_s", "integer"),
        f("data.aps[].client_count", "integer"),
        f("data.aps[].satisfaction_pct", "integer"),
        f("data.aps[].uplink_type", "string"),
        f("data.aps[].uplink_speed_mbps", "integer"),
        f("data.aps[].radios", "array", nullable=False),
        f("data.aps[].radios[].band", "string", nullable=False),
        f("data.aps[].radios[].channel", "integer"),
        f("data.aps[].radios[].channel_setting", "string"),
        f("data.aps[].radios[].width_mhz", "integer"),
        f("data.aps[].radios[].tx_power_dbm", "integer"),
        f("data.aps[].radios[].tx_power_mode", "string"),
        f("data.aps[].radios[].channel_utilization_pct", "integer"),
        f("data.aps[].radios[].self_rx_pct", "integer"),
        f("data.aps[].radios[].self_tx_pct", "integer"),
        f("data.aps[].radios[].client_count", "integer"),
        f("data.aps[].radios[].satisfaction_pct", "integer"),
        f("data.aps[].radios[].tx_retry_pct_cumulative", "number"),
        f("data.clients", "array", nullable=False),
        f("data.clients[].id", "string", nullable=False,
          identifier=True),
        f("data.clients[].ip", "string", identifier=True),
        f("data.clients[].name", "string", untrusted=True),
        f("data.clients[].vendor", "string", untrusted=True),
        f("data.clients[].private_mac", "boolean", nullable=False),
        f("data.clients[].ssid", "string", untrusted=True),
        f("data.clients[].ap_id", "string", identifier=True),
        f("data.clients[].ap_name", "string", untrusted=True),
        f("data.clients[].band", "string", nullable=False),
        f("data.clients[].channel", "integer"),
        f("data.clients[].wifi_standard", "string"),
        f("data.clients[].wifi_standard_raw", "string"),
        f("data.clients[].signal_dbm", "integer"),
        f("data.clients[].noise_dbm", "integer"),
        f("data.clients[].tx_rate_mbps", "number"),
        f("data.clients[].rx_rate_mbps", "number"),
        f("data.clients[].satisfaction_pct", "integer"),
        f("data.clients[].tx_retry_pct_cumulative", "number"),
        f("data.clients[].tx_retry_pct_recent", "number"),
        f("data.clients[].connected_s", "integer"),
        f("data.clients[].idle_s", "integer"),
        f("data.clients[].guest", "boolean"),
        f("source.kind", "string", nullable=False),
        f("source.flavor", "string", nullable=False),
        f("source.controller_version", "string"),
        f("source.site", "string", nullable=False),
    )


class UniFiWifi(sdk.Module):
    """UniFi WiFi radio metrics from a local controller."""

    id = "unifi_wifi"
    title = "UniFi WiFi radio metrics"
    version = "0.1.0"
    config = {
        "base_url": sdk.Setting(
            type="string", required=True, pattern=_BASE_URL_PATTERN
        ),
        "site": sdk.Setting(
            type="string", default="default", pattern=_SITE_PATTERN
        ),
        "flavor": sdk.Setting(
            type="string", default="auto",
            enum=("auto", _UNIFI_OS, _CLASSIC),
        ),
        "auth": sdk.Setting(
            type="object",
            required=True,
            properties={
                "username": sdk.Setting(type="string", required=True),
                "password_secret": sdk.Setting(
                    type="string", required=True
                ),
            },
        ),
        "tls": sdk.TLS_SETTING,
        "request_timeout_s": sdk.Setting(
            type="number", default=15, minimum=1, maximum=60
        ),
    }
    http_allow = (
        sdk.Allow("GET", r"/"),
        sdk.Allow("POST", _LOGIN_OS),
        sdk.Allow("POST", _LOGIN_CLASSIC),
        sdk.Allow("POST", _LOGOUT_OS),
        sdk.Allow("POST", _LOGOUT_CLASSIC),
        sdk.Allow("GET", r"/logout"),
        sdk.Allow(
            "GET",
            r"/proxy/network/api/s/" + _SITE_PATTERN + r"/stat/sysinfo",
        ),
        sdk.Allow(
            "GET",
            r"/proxy/network/api/s/" + _SITE_PATTERN + r"/stat/device",
        ),
        sdk.Allow(
            "GET",
            r"/proxy/network/api/s/" + _SITE_PATTERN + r"/stat/sta",
        ),
        sdk.Allow(
            "GET", r"/api/s/" + _SITE_PATTERN + r"/stat/sysinfo"
        ),
        sdk.Allow(
            "GET", r"/api/s/" + _SITE_PATTERN + r"/stat/device"
        ),
        sdk.Allow("GET", r"/api/s/" + _SITE_PATTERN + r"/stat/sta"),
    )
    commands = (
        sdk.Command(
            name="snapshot",
            handler="snapshot",
            description=_SNAPSHOT_DESCRIPTION,
            optional={
                "label": sdk.Param(
                    "string", "Note about this reading.", max_len=200
                ),
            },
            timeout_ms=_SNAPSHOT_TIMEOUT_MS,
            fields=_fields(),
        ),
        sdk.Command(
            name="get",
            handler="get",
            description=_GET_DESCRIPTION,
            required={
                "id": sdk.Param("string", "Saved snapshot id to load."),
            },
            optional={
                "section": sdk.Param(
                    "string",
                    "Snapshot list to page.",
                    enum=("aps", "clients"),
                ),
                "offset": sdk.Param(
                    "integer", "Items to skip.", default=0, minimum=0
                ),
            },
            timeout_ms=_GET_TIMEOUT_MS,
            fields=_fields(),
        ),
    )

    def check(self, ctx: sdk.Context) -> list:
        """Read-only doctor check: login, sysinfo, logout."""
        flavor, session, logout_path = _connect(ctx)
        try:
            _read_stat(session, _stat_path(flavor, ctx, "sysinfo"))
        finally:
            _logout(session, logout_path, ctx)
        return ["login ok", "site ok", "logout ok"]

    def snapshot(self, ctx: sdk.Context, params: dict) -> sdk.Result:
        """Log in, read sysinfo/devices/clients, log out, save."""
        flavor, session, logout_path = _connect(ctx)
        try:
            sysinfo = _read_stat(
                session, _stat_path(flavor, ctx, "sysinfo")
            )
            devices = _read_stat(
                session, _stat_path(flavor, ctx, "device")
            )
            stations = _read_stat(
                session, _stat_path(flavor, ctx, "sta")
            )
        finally:
            _logout(session, logout_path, ctx)
        aps, ap_warnings = normalise.normalise_aps(devices)
        clients, client_warnings = normalise.normalise_clients(
            stations, aps
        )
        summary, summary_warnings = normalise.build_summary(aps, clients)
        data = {"summary": summary, "aps": aps, "clients": clients}
        compact = {
            "summary": summary,
            "aps": aps,
            "clients": [normalise.compact_client(item) for item in clients],
        }
        version = None
        if sysinfo:
            first = sysinfo[0]
            if isinstance(first, dict):
                raw = first.get("version")
                version = normalise._strict_word(
                    raw, normalise._MODEL_RE
                )
        source = {
            "kind": "unifi",
            "flavor": flavor,
            "controller_version": version,
            "site": ctx.config["site"],
        }
        return sdk.Result(
            data=data,
            source=source,
            compact=compact,
            paging=sdk.Paging(command="get", lists=("clients",)),
            warnings=(
                list(ap_warnings)
                + list(client_warnings)
                + list(summary_warnings)
            ),
            label=params.get("label"),
            save="snapshot",
        )

    def get(self, ctx: sdk.Context, params: dict) -> sdk.Result:
        """Load a saved snapshot, whole or one list section from offset.

        Saved files are re-validated with the collection rules before
        anything is returned: a locally edited file can smuggle control
        characters into plain-string fields the runner does not
        sanitise. Paging offsets index the saved lists, so any unreadable
        or id-less record fails the whole ``get`` as damaged instead of
        being dropped (which would shift the offsets). The whole snapshot
        recomputes its summary from the re-validated records.
        """
        try:
            saved = ctx.store.load(params["id"])
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code not in sdk.ERROR_CODES:
                code = "upstream_unexpected"
            raise sdk.CommandError(
                code, "Cannot load that saved snapshot."
            ) from None
        data = saved.get("data") if isinstance(saved, dict) else None
        if not isinstance(data, dict):
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot load that saved snapshot."
            )
        summary = data.get("summary")
        aps = data.get("aps")
        clients = data.get("clients")
        if (
            not isinstance(summary, dict)
            or not isinstance(aps, list)
            or not isinstance(clients, list)
        ):
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot load that saved snapshot."
            )
        try:
            source = normalise.revalidate_source(saved.get("source"))
        except ValueError as exc:
            raise sdk.CommandError(
                "upstream_unexpected", str(exc)
            ) from None
        collected_at = _parse_collected_at(saved.get("collected_at"))
        if collected_at is None:
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot load that saved snapshot."
            )
        label = saved.get("label")
        if label is not None and not isinstance(label, str):
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot load that saved snapshot."
            )
        if not normalise.saved_ids_intact(aps, clients):
            raise sdk.CommandError(
                "upstream_unexpected", normalise.DAMAGED_SNAPSHOT
            )
        if not normalise.saved_radios_intact(aps):
            raise sdk.CommandError(
                "upstream_unexpected", normalise.DAMAGED_SNAPSHOT
            )
        try:
            restated, ssid_omitted = normalise.restated_warnings(saved)
        except ValueError as exc:
            raise sdk.CommandError(
                "upstream_unexpected", str(exc)
            ) from None
        section = params.get("section")
        offset = params.get("offset", 0)
        if section is None:
            if offset > 0:
                raise sdk.CommandError(
                    "bad_params",
                    "An offset without a section has no list to skip.",
                )
            clean_aps, ap_warnings = normalise.revalidate_aps(aps)
            clean_clients, client_warnings = normalise.revalidate_clients(
                clients, clean_aps
            )
            clean_summary, _ = normalise.build_summary(
                clean_aps, clean_clients
            )
            try:
                clean_summary["clients_by_ssid"] = (
                    normalise.validated_ssid_groups(
                        summary.get("clients_by_ssid"),
                        clean_clients,
                        ssid_omitted,
                    )
                )
            except ValueError as exc:
                raise sdk.CommandError(
                    "upstream_unexpected", str(exc)
                ) from None
            page = {
                "summary": clean_summary,
                "aps": clean_aps,
                "clients": clean_clients,
            }
            compact = {
                "summary": clean_summary,
                "aps": clean_aps,
                "clients": [
                    normalise.compact_client(item)
                    for item in clean_clients
                ],
            }
            return sdk.Result(
                data=page,
                source=source,
                compact=compact,
                paging=sdk.Paging(
                    command="get",
                    lists=("clients",),
                    params={"id": params["id"]},
                ),
                warnings=_combined_warnings(
                    restated, ap_warnings, client_warnings
                ),
                label=label,
                collected_at=collected_at,
            )
        clean_aps, section_ap_warnings = normalise.revalidate_aps(aps)
        clean_clients, section_client_warnings = (
            normalise.revalidate_clients(clients, clean_aps)
        )
        items = clean_aps if section == "aps" else clean_clients
        page_items = list(items[offset:])
        out: dict[str, Any] = {section: page_items}
        compact_out: dict[str, Any] | None = None
        if section == "clients":
            compact_out = {
                "clients": [
                    normalise.compact_client(item)
                    for item in page_items
                ]
            }
        return sdk.Result(
            data=out,
            source=source,
            compact=compact_out,
            paging=sdk.Paging(
                command="get",
                lists=(section,),
                params={"id": params["id"]},
                start={section: offset},
            ),
            warnings=_combined_warnings(
                restated, section_ap_warnings, section_client_warnings
            ),
            label=label,
            collected_at=collected_at,
        )


def _combined_warnings(
    restated: Any, ap_warnings: Any, client_warnings: Any
) -> list:
    """Saved-order-first warnings: restated saved text, then any fresh
    re-validation findings, de-duplicated (D3)."""
    combined = list(restated)
    for warning in list(ap_warnings) + list(client_warnings):
        if warning not in combined:
            combined.append(warning)
    return combined


def _stat_path(flavor: str, ctx: sdk.Context, endpoint: str) -> str:
    if flavor == _UNIFI_OS:
        prefix = _SITE_PREFIX_OS
    else:
        prefix = _SITE_PREFIX_CLASSIC
    return (prefix % ctx.config["site"]) + "/stat/" + endpoint


def _login_path(flavor: str) -> str:
    if flavor == _UNIFI_OS:
        return _LOGIN_OS
    return _LOGIN_CLASSIC


def _logout_path(flavor: str) -> str:
    if flavor == _UNIFI_OS:
        return _LOGOUT_OS
    # U1: classic consoles accept POST /api/logout; older ones only
    # GET /logout. This run uses the POST form.
    return _LOGOUT_CLASSIC


def _connect(ctx: sdk.Context) -> tuple:
    """Detect the flavour, log in, and return (flavor, session, logout)."""
    password = ctx.secret(ctx.config["auth"]["password_secret"])
    username = ctx.config["auth"]["username"]
    session = ctx.http(
        ctx.config["base_url"],
        tls=ctx.config.get("tls"),
        timeout_s=ctx.config.get("request_timeout_s"),
    )
    wanted = ctx.config.get("flavor", "auto")
    try:
        if wanted == "auto":
            try:
                flavor = _detect_flavor(session)
            except sdk.NotAllowed:
                # The core HTTP client currently refuses the bare root
                # path, so GET / detection cannot run. Never fall back to
                # trying both login paths: that would send the password
                # twice and add a failed login to the controller's audit
                # log. The owner sets the flavour instead.
                raise sdk.CommandError(
                    "config_invalid",
                    "Automatic controller detection is not available yet; "
                    "set unifi_wifi.flavor to unifi_os or classic.",
                ) from None
        else:
            flavor = wanted
        response = session.post(
            _login_path(flavor),
            json={"username": username, "password": password},
        )
        if response.status == 200:
            return flavor, session, _logout_path(flavor)
        if response.status == 400:
            # U1: classic consoles answer bad credentials with 400
            # api.err.Invalid. Treated as an auth failure, never retried.
            raise sdk.CommandError(
                "upstream_auth", "The controller refused the login."
            )
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        )
    except sdk.UpstreamError as exc:
        session.clear_auth()
        session.close()
        if exc.code == "upstream_tls":
            # The first request is where TLS fails. The core message is
            # fixed text (no upstream content); add what the owner can set.
            raise sdk.CommandError("upstream_tls", _TLS_HINT % exc.message) from None
        raise
    except Exception:
        session.clear_auth()
        session.close()
        raise


_TLS_HINT = (
    "TLS check failed (%s). Set unifi_wifi.tls.fingerprint_sha256 to pin "
    "the controller's certificate, or tls.ca_file (with "
    "tls.server_hostname for an IP address), or tls.insecure."
)


def _detect_flavor(session: Any) -> Any:
    """One GET / probe: 200 means unifi_os, 3xx means classic.

    Anything else is an upstream_unexpected error with fixed text;
    the controller's own message is never repeated.
    """
    try:
        response = session.get("/")
    except sdk.UpstreamError as exc:
        if exc.status is not None and 300 <= exc.status <= 399:
            return _CLASSIC
        if exc.code in ("upstream_tls", "timeout", "upstream_unreachable"):
            raise
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        ) from None
    if response.status == 200:
        return _UNIFI_OS
    raise sdk.CommandError(
        "upstream_unexpected",
        "The controller answered in an unexpected way.",
    )


def _read_stat(session: Any, path: str) -> list:
    """GET one stat endpoint and return its ``data`` list.

    Non-200 statuses, a non-``ok`` envelope, or a non-list ``data``
    are all ``upstream_unexpected`` with fixed text. 401/403 and 3xx
    arrive as UpstreamError and propagate untouched.
    """
    response = session.get(path)
    if response.status != 200:
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        )
    meta = payload.get("meta")
    if not isinstance(meta, dict) or meta.get("rc") != "ok":
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise sdk.CommandError(
            "upstream_unexpected",
            "The controller answered in an unexpected way.",
        )
    return data


def _logout(session: Any, path: str, ctx: sdk.Context) -> None:
    """Attempt logout; a failure is logged and never replaces the result."""
    try:
        response = session.post(path)
        if response.status != 200:
            ctx.log.info("UniFi logout failed")
    except Exception:
        ctx.log.info("UniFi logout failed")
    finally:
        session.clear_auth()
        session.close()


def _parse_collected_at(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)
