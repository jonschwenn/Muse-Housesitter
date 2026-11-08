# Module spec: `conformance_demo` — demo sensor hub (test only)

Status: **test-only**. This module is not installed and never ships. It
exists so the conformance suite in `tests/conformance/` has a module to
run every check against. Its fixtures live in
`tests/fixtures/conformance_demo/` and its manifest is
`tests/fixtures/conformance_demo/conformance.json`.

Schema: `housesitter.conformance_demo.snapshot/1`,
`housesitter.conformance_demo.get/1`,
`housesitter.conformance_demo.status/1`

## 1. Purpose

Give the conformance suite an invented "networked sensor hub" with:

- identifier fields (sensor ids, addresses), untrusted text (names),
  readings with units, and nulls for missing values
- a login/logout flow for the `status` command
- enough sensors in the large and hostile fixtures to exercise truncation
  and paging

## 2. Upstream

The hub speaks plain HTTP in tests. Config `base_url` points at it (the
fixture harness substitutes the test server's port). Auth is a username
plus a password read from a secret file. The module logs in only for
`status`, and always logs out again.

### HTTP allow-list (complete)

```
GET   /api/v1/sensors-example
GET   /api/v1/status-example
POST  /api/login
POST  /api/logout
```

### TLS

Standard `tls` object config. The fixture server speaks plain HTTP, so
the harness drops the TLS config.

## 3. Commands

| Command | Params | Does |
|---------|--------|------|
| `housesitter.conformance_demo.snapshot` | `label?: string` (≤ 200 chars) | Reads the sensor list. Saves the full snapshot. Returns it, compacted or paged if over budget. |
| `housesitter.conformance_demo.get` | `id: string` (required), `section?: string` (`sensors`), `offset?: integer` (default 0) | Loads a saved snapshot and returns its sensors from the offset. No network. Pages its own output again if still over budget. |
| `housesitter.conformance_demo.status` | none | Logs in, reads hub status, logs out. |

## 4. Output schema

### Conventions

- Temperatures are in degrees Celsius, humidity in percent, uptime in
  seconds.
- Unknown values are `null`, never `0` or `""`.
- Fields marked **Identifier** are returned as they are by default, and
  left out when `identifiers` is `none`. Fields marked **Untrusted** are
  sanitised and capped at 64 characters.

`data`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `sensors` | array | — | derived | Every sensor in the snapshot, in hub order. (`snapshot`, `get`) |
| `sensor_count` | integer | — | derived | Total sensors in the snapshot. In `get` pages this is the total, not the page size. (`snapshot`, `get`, `status`) |
| `firmware` | string\|null | — | `firmware` | Hub firmware version, as reported. **Untrusted.** (`status` only) |
| `uptime_s` | integer\|null | s | `uptime_s` | Seconds since the hub booted. (`status` only) |

`data.sensors[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `id` | string\|null | — | `id` | Sensor identity (a MAC address). **Identifier.** |
| `name` | string\|null | — | `name` | Sensor name as set in the hub. **Untrusted.** |
| `ip` | string\|null | — | `ip` | Sensor network address. **Identifier.** |
| `ssid` | string\|null | — | `ssid` | Network name the sensor is on. **Untrusted.** |
| `temperature_c` | number\|null | °C | `temperature_c` | Last temperature reading. |
| `humidity_pct` | integer\|null | % | `humidity_pct` | Last humidity reading. |
| `online` | boolean\|null | — | `online` | Whether the hub sees the sensor as reachable. |

### Compact form and paging

- The compact form drops `ssid` and `ip` from each sensor.
- If the result is still over budget, `sensors` is truncated and `more`
  points at `get` with the saved id, `section: "sensors"`, and the next
  offset.

`source`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `kind` | string | — | constant | Always `"sensor_hub"`. |
| `api_version` | string\|null | — | `api_version` | Hub API version, as reported. **Untrusted** (capped at 32). |

## 5. Config

```json
"conformance_demo": {
  "enabled": true,
  "base_url": "http://192.0.2.10",
  "auth": {"username": "housesitter-example", "password_secret": "sensor_password"},
  "tls": {}
}
```

`password_secret` names a file in the secrets directory holding the hub
password.
