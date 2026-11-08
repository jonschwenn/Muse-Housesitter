"""Conformance demo module (test-only, never installed).

An invented "networked sensor hub" used only by the conformance suite in
``tests/conformance/``. It exercises every conformance check: identifiers,
untrusted text, paging over the size budget, login/logout, and secret
handling. Nothing under ``src/`` may import it.
"""

from housesitter import sdk


def _text(value):
    """A string value, else None (missing upstream data stays null)."""
    return value if isinstance(value, str) else None


def _number(value):
    """An int or float value (never a bool), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _integer(value):
    """An int value (never a bool or float), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _boolean(value):
    """A bool value, else None."""
    return value if isinstance(value, bool) else None


def _normalise_sensor(item):
    """Allow-list one upstream sensor record into documented fields."""
    return {
        "id": _text(item.get("id")),
        "name": _text(item.get("name")),
        "ip": _text(item.get("ip")),
        "ssid": _text(item.get("ssid")),
        "temperature_c": _number(item.get("temperature_c")),
        "humidity_pct": _integer(item.get("humidity_pct")),
        "online": _boolean(item.get("online")),
    }


def _compact_sensor(item):
    """The smaller per-sensor form: readings without network addresses."""
    return {
        "id": item["id"],
        "name": item["name"],
        "temperature_c": item["temperature_c"],
        "humidity_pct": item["humidity_pct"],
        "online": item["online"],
    }


_SENSOR_FIELDS = (
    sdk.Field("data.sensors", "array", nullable=False),
    sdk.Field("data.sensors[].id", "string", identifier=True),
    sdk.Field("data.sensors[].name", "string", untrusted=True),
    sdk.Field("data.sensors[].ip", "string", identifier=True),
    sdk.Field("data.sensors[].ssid", "string", untrusted=True),
    sdk.Field("data.sensors[].temperature_c", "number"),
    sdk.Field("data.sensors[].humidity_pct", "integer"),
    sdk.Field("data.sensors[].online", "boolean"),
    sdk.Field("data.sensor_count", "integer"),
    sdk.Field("source.kind", "string"),
    sdk.Field("source.api_version", "string", untrusted=True, max_len=32),
)


class ConformanceDemo(sdk.Module):
    """Invented sensor hub for the conformance suite (test only)."""

    id = "conformance_demo"
    title = "Conformance demo sensors (test only)"
    version = "0.1.0"
    config = {
        "base_url": sdk.Setting(type="string", default="http://192.0.2.10"),
        "auth": sdk.Setting(
            type="object",
            required=True,
            properties={
                "username": sdk.Setting(type="string", required=True),
                "password_secret": sdk.Setting(type="string", required=True),
            },
        ),
        "tls": sdk.TLS_SETTING,
    }
    http_allow = (
        sdk.Allow("GET", r"/api/v1/sensors-example"),
        sdk.Allow("GET", r"/api/v1/status-example"),
        sdk.Allow("POST", r"/api/login"),
        sdk.Allow("POST", r"/api/logout"),
    )
    commands = (
        sdk.Command(
            name="snapshot",
            handler="snapshot",
            description=(
                "Read every sensor from the sensor hub: id, name, network "
                "address, readings, and reachability. Read-only. Saves the "
                "full snapshot locally."
            ),
            optional={
                "label": sdk.Param(
                    "string", "Note about this reading.", max_len=200
                ),
            },
            fields=_SENSOR_FIELDS,
        ),
        sdk.Command(
            name="get",
            handler="get",
            description=(
                "Return one saved sensor snapshot, or one page of its "
                "sensor list. Read-only. Uses the local store, no network."
            ),
            required={
                "id": sdk.Param("string", "Saved snapshot id to load."),
            },
            optional={
                "section": sdk.Param(
                    "string",
                    "Sensor list to page.",
                    enum=("sensors",),
                ),
                "offset": sdk.Param(
                    "integer", "Items to skip.", minimum=0
                ),
            },
            timeout_ms=10_000,
            fields=_SENSOR_FIELDS,
        ),
        sdk.Command(
            name="status",
            handler="status",
            description=(
                "Log in to the sensor hub, read its firmware version and "
                "uptime, then log out. Read-only."
            ),
            fields=(
                sdk.Field("data.firmware", "string", untrusted=True),
                sdk.Field("data.uptime_s", "integer"),
                sdk.Field("data.sensor_count", "integer"),
                sdk.Field("source.kind", "string"),
                sdk.Field("source.api_version", "string", untrusted=True, max_len=32),
            ),
        ),
    )

    def snapshot(self, ctx, params):
        """Read the sensor list, keeping only documented fields."""
        with ctx.http(ctx.config["base_url"], tls=ctx.config.get("tls")) as session:
            response = session.get("/api/v1/sensors-example")
        if response.status != 200:
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot read the sensor hub."
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise sdk.CommandError(
                "upstream_unexpected", "Sensor hub returned an unexpected shape."
            )
        raw = payload.get("sensors")
        if not isinstance(raw, list):
            raise sdk.CommandError(
                "upstream_unexpected", "Sensor hub returned an unexpected shape."
            )
        sensors = [_normalise_sensor(item) for item in raw if isinstance(item, dict)]
        missing = sum(1 for item in sensors if item["temperature_c"] is None)
        warnings = []
        if missing == 1:
            warnings.append("1 sensor reported no temperature")
        elif missing > 1:
            warnings.append(f"{missing} sensors reported no temperature")
        data = {"sensors": sensors, "sensor_count": len(sensors)}
        return sdk.Result(
            data=data,
            source={
                "kind": "sensor_hub",
                "api_version": _text(payload.get("api_version")),
            },
            compact={
                "sensors": [_compact_sensor(item) for item in sensors],
                "sensor_count": len(sensors),
            },
            paging=sdk.Paging(command="get", lists=("sensors",)),
            warnings=warnings,
            label=params.get("label"),
            save="snapshot",
        )

    def get(self, ctx, params):
        """Load a saved snapshot and return its sensors from the offset."""
        try:
            saved = ctx.store.load(params["id"])
        except Exception as exc:  # noqa: BLE001 - mapped to a stable code below
            code = getattr(exc, "code", None)
            if code not in sdk.ERROR_CODES:
                code = "upstream_unexpected"
            raise sdk.CommandError(code, "Cannot load that saved snapshot.") from None
        data = saved.get("data") if isinstance(saved, dict) else None
        sensors = (
            data.get("sensors") if isinstance(data, dict) else None
        )
        if not isinstance(sensors, list):
            raise sdk.CommandError(
                "upstream_unexpected", "Cannot load that saved snapshot."
            )
        offset = params.get("offset", 0)
        page = sensors[offset:]
        return sdk.Result(
            data={"sensors": page, "sensor_count": len(sensors)},
            source=saved.get("source", {}),
            paging=sdk.Paging(
                command="get",
                lists=("sensors",),
                params={"id": params["id"]},
                start={"sensors": offset},
            ),
        )

    def status(self, ctx, params):
        """Log in, read hub status, and always log out again."""
        del params
        password = ctx.secret(ctx.config["auth"]["password_secret"])
        username = ctx.config["auth"]["username"]
        with ctx.http(ctx.config["base_url"], tls=ctx.config.get("tls")) as session:
            try:
                login = session.post(
                    "/api/login",
                    json={"username": username, "password": password},
                )
                if login.status != 200:
                    raise sdk.CommandError(
                        "upstream_unexpected", "Sensor hub refused the login."
                    )
                response = session.get("/api/v1/status-example")
                if response.status != 200:
                    raise sdk.CommandError(
                        "upstream_unexpected",
                        "Cannot read the sensor hub status.",
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise sdk.CommandError(
                        "upstream_unexpected",
                        "Sensor hub returned an unexpected shape.",
                    )
                return sdk.Result(
                    data={
                        "firmware": _text(payload.get("firmware")),
                        "uptime_s": _integer(payload.get("uptime_s")),
                        "sensor_count": _integer(payload.get("sensor_count")),
                    },
                    source={
                        "kind": "sensor_hub",
                        "api_version": _text(payload.get("api_version")),
                    },
                )
            finally:
                # Logout must never replace the real result or error: a
                # 401/403 here raises UpstreamError (review finding 6).
                try:
                    session.post("/api/logout")
                except sdk.UpstreamError:
                    ctx.log.info("sensor hub logout failed")
