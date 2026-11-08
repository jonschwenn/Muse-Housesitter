# Module spec: `unifi_wifi` — UniFi WiFi radio metrics

Status: implemented on branch unifi-wifi-module (0.1.0); open questions in §7 await Jon.
Schemas: `housesitter.unifi_wifi.snapshot/1` and `housesitter.unifi_wifi.get/1`

## 1. Purpose

Give Muse an accurate picture of the home's WiFi radio conditions from a
local UniFi Network controller:

- which access points (APs) there are
- what channels and widths their radios use, and how busy those channels are
- which wireless clients are on which AP and band
- how strong each client's signal is
- what rates the clients negotiated, and how often transmissions are retried

The module gathers and labels these numbers. Muse interprets them.

### Questions this lets Muse answer

- "What's connected right now, and how well?"
- "Did moving the speakers to the IoT network help?" Muse compares two
  labelled snapshots.
- "Which devices are still weak or stuck on 2.4 GHz?"
- "Is a channel congested on one AP?"

### What it cannot see

- Wired clients.
- Throughput actually achieved. Negotiated rate is a *floor* for what the
  link could do, not a measurement of real speed.
- The device's own capability. A phone on 2.4 GHz may or may not support
  5 GHz.
- Anything between snapshots.
- Non-UniFi APs.
- Signal as the *client* hears the AP. UniFi reports signal as the AP
  hears the client.

## 2. Upstream

### 2.1 Account

A **local** UniFi account, created for Housesitter only, with the **View
Only** role, no remote/cloud access, and no SSO. The password lives in
`~/.config/housesitter/secrets/unifi_password` (mode 600).

### 2.2 Controller flavours

`flavor` is `auto`, `unifi_os`, or `classic`.

| Flavour | Default port | Login | Logout | Data prefix |
|---------|-------------|-------|--------|-------------|
| `unifi_os` (UDM, UCG, UX, Cloud Key Gen2+) | 443 | `POST /api/auth/login` | `POST /api/auth/logout` | `/proxy/network/api/s/<site>/` |
| `classic` (self-hosted Network application) | 8443 | `POST /api/login` | `POST /api/logout` (U1) | `/api/s/<site>/` |

- **Detection (`auto`): Planned (needs a core change; pending Jon's decision).**
  The module implements a single `GET /` probe without following redirects:
  200 means `unifi_os`, 3xx means `classic`, and anything else is
  `upstream_unexpected`. This upstream behaviour is unverified until Jon's
  live check (U1, issue #16). The core HTTP client currently refuses the
  bare root path `/`, so `flavor: "auto"` fails with `config_invalid`
  ("set unifi_wifi.flavor to unifi_os or classic") before any request is
  sent. Set `flavor` explicitly. The module never falls back to trying both
  login paths, because that would send the password twice. See D-auto (§7).
- **CSRF:** the core HTTP client, not the module, keeps cookies and CSRF
  (cross-site request forgery) tokens in memory only. It accepts
  `X-CSRF-Token` and `X-Updated-CSRF-Token`, keeps the latest token, and
  sends it on **non-GET** requests (logout).
- **Classic login and logout (U1):** a login response of 400 is treated as
  bad credentials (`upstream_auth`). Classic logout uses `POST /api/logout`;
  `GET /logout` remains on the allow-list but is unused. These upstream
  behaviours are unverified until Jon's live check (issue #16).
- **Logout** is always attempted after a successful login, including when a
  later step fails. If the process is killed (SDK timeout), logout cannot
  run, and the session expires on the controller.
- **No retries on auth failure.** Logins are audited by the controller, and
  repeated failures can lock the account.

### 2.3 HTTP allow-list (complete)

Paths are `fullmatch`ed after normalisation. Query strings and `%`-escapes
are refused, and redirects are never followed (ARCHITECTURE §3).

```
GET   /                                  flavour detection probe (currently blocked by core)
POST  /api/auth/login                    POST  /api/login
POST  /api/auth/logout                   POST  /api/logout      GET /logout
GET   {prefix}stat/sysinfo               controller version
GET   {prefix}stat/device                APs and their radios
GET   {prefix}stat/sta                   currently associated clients
```

These endpoints are **deliberately not fetched**:

- `rest/wlanconf` returns WLAN passphrases (`x_passphrase`).
- `rest/*` in general is the configuration surface.
- Anything under `cmd/` is commands, i.e. writes.
- `stat/alluser` is the historical client list. It is not "now."

`{prefix}` is `/proxy/network/api/s/<site>/` or `/api/s/<site>/`, where
`<site>` must match `[A-Za-z0-9_-]{1,64}`. Config validation checks it, and
the allow-list regex enforces it again.

### 2.4 TLS

Choose one in config `tls`:

- `{"fingerprint_sha256": "..."}`: pin the leaf certificate. This is
  recommended for self-signed consoles reached by IP. The pin is checked
  before any request bytes (including the login body) are sent.
- `{"ca_file": "/path", "server_hostname": "unifi.local"}`: verify against
  that CA. `server_hostname` is needed when the `base_url` host is an IP
  that isn't in the certificate.
- `{"insecure": true}`: no verification. Every envelope then carries a
  warning.

There is no silent default to insecure. With no `tls` config, normal system
verification applies. That fails on self-signed consoles with a clear
`upstream_tls` error that names the three `tls` options above.
**Planned (core):** print the certificate's fingerprint for pinning on a
verification failure; the core currently does not print it. The fingerprint
is not a secret.

## 3. Commands

Commands are declared with the short names `snapshot` and `get`, under
`housesitter.unifi_wifi.`. The runner names each envelope schema per command:
`snapshot` returns `housesitter.unifi_wifi.snapshot/1`, and `get` returns
`housesitter.unifi_wifi.get/1`. Both use the data fields in §4; `get` returns
only `aps` or only `clients` when `section` is given.

| Command | Params | Timeout | Does |
|---------|--------|---------|------|
| `housesitter.unifi_wifi.snapshot` | `label?: string` (≤ 200 chars) | 60 s | Logs in, reads sysinfo, devices, and clients, logs out. Saves the full snapshot. Returns it, compacted or paged if over budget. At most 6 requests, and each request's timeout is the smaller of `request_timeout_s` and the time left before the deadline. |
| `housesitter.unifi_wifi.list` | `offset?: integer` (default 0), `limit?: integer` (default 20, max 100) | 10 s (proposed) | **Not built — pending Jon's decision (Q1/Q2, §7)**. Proposed: lists **all** saved snapshots, newest first and paged: `id`, `collected_at`, `label`, `ap_count`, `client_count`, plus `total`. No network. |
| `housesitter.unifi_wifi.get` | `id: string` (required), `section?: string` (`aps` or `clients`), `offset?: integer` (min 0, default 0); `limit?: integer` (default 100, max 200): **Not built — pending Jon's decision (Q1/Q2, §7)** | 10 s | Returns a saved snapshot, or one section of it, paged, with the identifier policy re-applied. No network. `id` must match the store's naming pattern and exist in this module's store, or the result is `not_found`. If the page is over budget, `more` points at the next page. |

`get` rejects a positive `offset` without `section` as `bad_params`. Paging
is by size budget only; there is no implemented `limit` parameter.

There is no `compare` command. Comparing snapshots is analysis, so Muse does
it (AGENTS rule 2). SKILL.md explains how.

## 4. Output schema (`data` of a snapshot)

### 4.0 Conventions

- Rates are in Mbps. UniFi reports kbps, so the module divides by 1000.
- Signal and power values are in dBm (decibels relative to one milliwatt).
  Percentages are 0–100; out-of-range upstream percentages become `null`.
  Upstream percentage fields accept only integral values; the cumulative
  retry percentage is calculated separately and is currently unrounded.
- Unknown values are `null`.
- **Band** is `"2.4GHz"`, `"5GHz"`, `"6GHz"`, or `"unknown"`, from UniFi's
  `radio`: `ng`→2.4, `na`→5, `6e`→6.
- Fields marked **Identifier** are returned as they are by default, and
  left out when `identifiers` is `none` (ARCHITECTURE §7). Fields marked **Untrusted** are sanitised and capped.
- Upstream numbers may arrive as strings (e.g. `ht: "80"`) or as words
  (e.g. `channel: "auto"`). The module parses numbers it can, and anything
  else becomes `null`. For radio settings, the raw word is kept in a
  `*_setting` field.
  Numbers larger than `2**53` in absolute value, and numeric strings longer
  than 32 characters after trimming surrounding whitespace, become `null`.
  A numeric string's limit is checked on its exact decimal value, before
  conversion. A fractional string so close to a whole number that it would
  round to one (e.g. `"100.000000000000001"`) also becomes `null`.
- During collection, AP and wireless client records that could not be read
  or had no valid colon-separated MAC address are left out, with a counted
  warning. MACs are normalised to lowercase.
- Controller strings that are not user-chosen are restricted: `model`,
  `firmware`, and `controller_version` match `[A-Za-z0-9._+-]{1,64}`;
  `wifi_standard_raw` matches `[A-Za-z0-9._-]{1,16}`. `channel_setting`
  keeps a parsed integer as text, or a word matching `[A-Za-z0-9]{1,16}`.
  `tx_power_mode` and `uplink_type` use the value lists below. Other values
  become `null`.

`data`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `aps` | list | — | `stat/device` | Access points (§4.1). |
| `clients` | list | — | `stat/sta` | Wireless clients (§4.2). |
| `summary` | object | — | derived from `aps` and `clients` | Counts and groupings (§4.3). |

### 4.1 APs

Only devices with `type == "uap"` are included. `online` is a non-null
boolean from parsed `state == 1`; missing `state` reads as offline (Q3, §7).
Duplicate AP MACs keep the first record, with a counted warning.

`data.aps[]`

| Field | Type | Unit | Source (`stat/device`) | Meaning |
|-------|------|------|------------------------|---------|
| `id` | string | — | `mac` | AP identity. **Identifier.** |
| `name` | string\|null | — | `name` | AP name as set in UniFi. **Untrusted.** |
| `model` | string\|null | — | `model` | Hardware model code. |
| `firmware` | string\|null | — | `version` | |
| `online` | bool | — | `state == 1` | Whether the controller sees it as connected. |
| `uptime_s` | int\|null | s | `uptime` | |
| `client_count` | int\|null | — | `num_sta` | |
| `satisfaction_pct` | int\|null | % | `satisfaction` | UniFi's own experience score, defined by Ubiquiti. |
| `uplink_type` | string\|null | — | `uplink.type` | `wire` or `wireless` (mesh). |
| `uplink_speed_mbps` | int\|null | Mbps | `uplink.speed` | Wired link speed to the switch. |
| `radios` | list | — | `radio_table` joined to `radio_table_stats` by `name` | One entry per radio, below. |

`data.aps[].radios[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `band` | string | — | `radio` | |
| `channel` | int\|null | — | stats `channel`, else table `channel` if numeric | Channel in use. |
| `channel_setting` | string\|null | — | table `channel` | Configured value, e.g. `"auto"` or `"36"`. |
| `width_mhz` | int\|null | MHz | table `ht`, parsed | Channel width. |
| `tx_power_dbm` | int\|null | dBm | stats `tx_power` | AP transmit power. |
| `tx_power_mode` | string\|null | — | table `tx_power_mode` | `auto`, `high`, `medium`, `low`, or `custom`. |
| `channel_utilization_pct` | int\|null | % | stats `cu_total` | How busy the channel is, counting every network the AP can hear. |
| `self_rx_pct` | int\|null | % | stats `cu_self_rx` | Airtime this AP spends receiving, as UniFi reports it (U1: confirm the denominator). |
| `self_tx_pct` | int\|null | % | stats `cu_self_tx` | Airtime this AP spends transmitting, as UniFi reports it. |
| `client_count` | int\|null | — | stats `num_sta` | |
| `satisfaction_pct` | int\|null | % | stats `satisfaction` | |
| `tx_retry_pct_cumulative` | float\|null | % | stats `tx_retries / (tx_packets + tx_retries)` × 100 | Since the counters reset. `null` if both are 0. |

### 4.2 Clients

Records with `is_wired` exactly true are excluded; a missing flag is treated
as wireless (unverified upstream assumption: U1, issue #16). Duplicate
client MACs keep the first record, with a counted warning. A client's
`ap_id` is an AP in `aps` or `null`; when it is `null`, `ap_name` is `null`
too.

`data.clients[]`

| Field | Type | Unit | Source (`stat/sta`) | Meaning |
|-------|------|------|---------------------|---------|
| `id` | string | — | `mac` | **Identifier.** |
| `ip` | string\|null | — | `ip` | **Identifier.** |
| `name` | string\|null | — | `name`, else `hostname` | **Untrusted.** |
| `vendor` | string\|null | — | `oui` | Manufacturer looked up from the MAC prefix. Helps identify the device type. **Untrusted.** |
| `private_mac` | bool | — | locally administered bit of `mac` | The device uses a randomised address, so it may appear with a different id later. |
| `ssid` | string\|null | — | `essid` | **Untrusted.** |
| `ap_id` | string\|null | — | `ap_mac` | **Identifier.** Joins to `aps[].id`. |
| `ap_name` | string\|null | — | resolved from `aps` | **Untrusted.** |
| `band` | string | — | `radio` | |
| `channel` | int\|null | — | `channel` | |
| `wifi_standard` | string\|null | — | `radio_proto`, mapped | One of `a`, `b`, `g`, `n`, `ac`, `ax`, `be`. UniFi's `na` and `ng` both mean 802.11n, so they map to `n`. Unrecognised values map to `null`. |
| `wifi_standard_raw` | string\|null | — | `radio_proto` | The unmapped value, kept for checking. |
| `signal_dbm` | int\|null | dBm | `signal` | How strongly the AP hears the client. Closer to 0 is stronger. |
| `noise_dbm` | int\|null | dBm | `noise` | If reported. |
| `tx_rate_mbps` | float\|null | Mbps | `tx_rate` / 1000 | Negotiated rate, AP → client. |
| `rx_rate_mbps` | float\|null | Mbps | `rx_rate` / 1000 | Negotiated rate, client → AP. |
| `satisfaction_pct` | int\|null | % | `satisfaction` | UniFi's score. |
| `tx_retry_pct_cumulative` | float\|null | % | `tx_retries / (tx_packets + tx_retries)` × 100 | Since this association. `null` if both are 0. |
| `tx_retry_pct_recent` | float\|null | % | `wifi_tx_retries_percentage` | UniFi's own retry figure, believed to cover a recent window (U1). `null` if absent. |
| `connected_s` | int\|null | s | `uptime` | Time since this association. |
| `idle_s` | int\|null | s | `idletime` | Time since last traffic. |
| `guest` | bool\|null | — | `is_guest` | |

The upstream field names above come from community API documentation
(Art-of-WiFi UniFi-API-client) and an independent review. Their presence,
encodings, and units **must be verified against one live controller of each
available flavour** before the schema is frozen (U1, issue #16; Jon's live
check). Fields that turn out to be missing stay in the schema as `null`.

### 4.3 Summary

These are counts only, with no judgement. Groupings are lists, never maps
keyed by untrusted names (ARCHITECTURE §7).

`data.summary`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `ap_count` | int | — | derived | APs in `aps`. |
| `ap_online_count` | int | — | derived | APs with `online` true. |
| `client_count` | int | — | derived | Clients in `clients`. |
| `clients_by_band` | list | — | derived | `[{band, count}]` for every band value, including zeros. |
| `clients_by_ap` | list | — | derived | `[{ap_id, ap_name, count}]`. `ap_id` is an **identifier** and `ap_name` is **untrusted**. |
| `clients_by_ssid` | list | — | derived | `[{ssid, count}]`. `ssid` is **untrusted**. A null SSID is its own entry. |

`data.summary.clients_by_band[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `band` | string | — | `clients[].band` | Every band in fixed order: `2.4GHz`, `5GHz`, `6GHz`, `unknown`. |
| `count` | int | — | derived | Clients on this band, including zero counts. |

`data.summary.clients_by_ap[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `ap_id` | string | — | `clients[].ap_id` | AP identity. **Identifier.** Groups sorted by `ap_id`; clients with null `ap_id` are excluded. |
| `ap_name` | string\|null | — | `clients[].ap_name` | AP name. **Untrusted.** |
| `count` | int | — | derived | Clients on this AP; only APs with clients have entries. |

`data.summary.clients_by_ssid[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `ssid` | string\|null | — | `clients[].ssid` | Network name. **Untrusted.** Sorted by SSID, with a null group last if any clients have a null SSID. |
| `count` | int | — | derived | Clients on this SSID. |

### 4.4 Order, compact form, paging

`get` re-validates saved records with the same rules used at collection.
An AP or client record that cannot be read or has no valid MAC `id`, or a
source with an invalid `kind`, `flavor`, or `site` (§4.5 and §5), fails the
whole command with `upstream_unexpected`, "That saved snapshot is damaged."
Records are not dropped, so paging offsets do not shift. Other invalid
values in nullable fields become `null`; an invalid band becomes `"unknown"`.
Whole-snapshot `get` recomputes `summary` from the re-validated records.

- `aps` are ordered by `name`, then `id`. `clients` are ordered by
  `ap_name`, then `name`, then `id`. The order is deterministic and doesn't
  rank anything. Null names sort last at each name comparison.
- Compact form drops these client fields: `noise_dbm`, `idle_s`, `vendor`,
  `wifi_standard_raw`, `guest`, `ip`, `tx_retry_pct_recent`.
- If the result is still over budget, `clients` is truncated and `more`
  points at `get` with `section: "clients"` and the next `offset`.
- Whole-snapshot output keeps `summary` and `aps` in full. Section-only
  `get` returns just that list, from `offset`; the runner pages it if needed.
- When `truncated` is true, call `more.command` with `more.params` exactly.

### 4.5 Source

`source`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `kind` | string | — | constant | `"unifi"` |
| `flavor` | string | — | detection or config | `unifi_os` or `classic` |
| `controller_version` | string\|null | — | `stat/sysinfo` `version` | |
| `site` | string | — | config | |

`host` is **not** included.

## 5. Config

```json
"unifi_wifi": {
  "enabled": true,
  "base_url": "https://192.0.2.1",
  "site": "default",
  "flavor": "unifi_os",
  "auth": { "username": "housesitter-readonly", "password_secret": "unifi_password" },
  "tls": { "fingerprint_sha256": "AB:CD:..." },
  "request_timeout_s": 15
}
```

`base_url` is required: `https://host` or `https://host:port`, HTTPS only,
with no path (an optional trailing `/` is accepted). There is no inferred
flavour-specific port; include the controller's port in the URL if needed.
An explicit port must be 1–65535, with no leading zeros.
This replaces the earlier `host` + `port` keys; pending Jon's decision
(C1): the conformance harness binds clients from `base_url`, and one URL
also settles which port `auto` would probe.

`site` defaults to `default` and matches `[A-Za-z0-9_-]{1,64}`. `flavor`
defaults to `auto` (`unifi_os` or `classic` must currently be set explicitly;
§2.2). `auth` requires a username and a `password_secret` naming the secret
file, never the password itself. `tls` uses the core options in §2.4.
`request_timeout_s` is a number from 1 to 60 seconds, default 15.

`doctor` calls `check()`: connect and log in, read this site's `stat/sysinfo`,
then attempt logout in `finally`. It returns `login ok`, `site ok`, and
`logout ok` after a successful read; the last message means logout was
attempted, not confirmed. Logout failures are logged with fixed text and
never replace the result. It never reads devices or clients or saves a
snapshot. Core config, secret, and TLS checks apply to the connection.

## 6. Tests

| Fixture (`tests/fixtures/unifi_wifi/`) | Purpose |
|----------------------------------------|---------|
| `unifi_os_*.json`, `classic_*.json` | Login responses, sysinfo, device, and sta for each flavour. The same invented house: 3 APs, 12 clients across all three bands, including a private-MAC phone and a 2.4 GHz-only device. |
| `edge_*.json` | Missing `radio_table_stats`, a null `signal`, an unknown `radio`, a `tx_packets` of 0 (no divide-by-zero), an AP that is offline. |
| `hostile_*.json` | Names that are 5 KB long, names with control, bidi, or zero-width characters, an injection-style name, two clients with the same name, `x_authkey` and `x_passphrase` in device records, an `X-CSRF-Token` value, and a controller error message that tries to inject text. The planted secrets must be absent from output, saved files, and logs, and the error text must be absent from stderr. |
| `large_sta.json` | 600 clients, to exercise compact form and paging. |

The tests check:

- Field-by-field normalisation for both flavours.
- `auto` fails before any request because the core refuses `/`; explicit
  flavours skip the probe. **Planned:** end-to-end `auto` detection tests
  (200, 3xx, other) after a core change and Jon's decision (D-auto).
- Core HTTP tests check that no redirect is followed and that the TLS pin
  is checked before the login body is sent.
- The size budget is measured with the SDK formula on non-ASCII-heavy names.
- Core HTTP tests check CSRF handling; module tests check logout after
  sysinfo HTTP errors, non-JSON device responses, sta API errors, and a
  sysinfo request timeout after login.
- Module tests cover bad credentials, non-JSON responses, and request
  timeouts; core HTTP tests cover connection refusal and TLS mismatch.
- The allow-list: calls to `rest/wlanconf`, `stat/device-basic`, `stat/sta?x=1`, and `%2e%2e` paths all raise `NotAllowed`, and site validation rejects `../`.
- `get` rejects ids outside the store's naming pattern, and ids that do not exist.
- All of the conformance suite.

## 7. Open items for this module

- **U1.** Verify the upstream field names and units on one real UniFi OS
  console and, if available, one classic controller. Use a tests/live run
  by Jon, with output not committed.
- **U2.** Should `snapshot` also read `stat/health`, for WAN and other
  subsystem status? It's useful context, but it isn't radio metrics.
  Proposed: no, it belongs in a later `unifi_health` module.
- **U3.** Neighbouring (rogue) AP scan via `stat/rogueap`. It's useful for
  channel planning, but it means reading other people's network names.
  Proposed: not in v1.
- **U4.** API-key auth (`X-API-KEY`) instead of a password, on Network
  versions that support it. It avoids password logins and lockout risk.
  Unknown: whether keys are accepted on the `stat/*` endpoints, or only on
  the newer Integration API, which has different paths and schema. The
  `auth` config object is shaped so that `{"api_key_secret": "..."}` can be
  added without breaking existing config.
- **Q1.** `get.limit` conflicts with the runner's budget-only paging:
  each follow-up page must return every remaining item after `offset`; a
  100-item cap would end the chain early with items left. Proposal: drop
  `limit`; paging is by size budget only. Pending Jon's decision.
- **Q2.** `list`'s output list key is unnamed. Proposal:
  `data.snapshots[]` (`id`, `collected_at`, `label`, `ap_count`, `client_count`)
  plus `data.total`. Pending Jon's decision; `list` is not built.
- **Q3.** `aps[].online` is a non-null bool from `state == 1`, so a missing
  `state` reads as offline. Proposal: allow null when `state` is missing.
  Pending Jon's decision.
- **Q4.** `tx_retry_pct_cumulative` precision is unspecified and currently
  unrounded. Proposal: round to 1 decimal. Pending Jon's decision.
- **C1.** Config uses `base_url` instead of `host` + `port` (§5).
  Pending Jon's decision.
- **D-auto.** Automatic flavour detection needs a core change (§2.2).
  **Planned**, pending Jon's decision.
