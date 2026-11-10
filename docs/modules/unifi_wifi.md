# Module spec: `unifi_wifi` — UniFi WiFi radio metrics

Status: implemented on branch unifi-wifi-module (0.1.0); open questions in §7 await Jon. Verified on UniFi OS, Network 10.6.x (U1, issue #16); classic unverified.
Schemas: `housesitter.unifi_wifi.snapshot/1` and `housesitter.unifi_wifi.get/1`

The schemas stay at `/1`: these additions and narrowing are not breaking
changes to `data` (ARCHITECTURE §4).

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
  `upstream_unexpected`. This upstream behaviour remains unverified
  (U1, issue #16). The core HTTP client currently refuses the
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
  behaviours remain unverified; no classic controller was available
  for Jon's live check (issue #16).
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
Collection warnings are repeated on whole and section-only `get`, including
the warning that TLS verification was disabled at collection (§4.4).

There is no `compare` command. Comparing snapshots is analysis, so Muse does
it (AGENTS rule 2). SKILL.md explains how.

## 4. Output schema (`data` of a snapshot)

### 4.0 Conventions

- Rates are in Mbps. UniFi reports kbps, so the module divides by 1000.
- Signal and power values are in dBm (decibels relative to one milliwatt).
  Percentages are 0–100; out-of-range upstream percentages become `null`.
  Upstream percentage fields accept only integral values; the cumulative
  retry percentage is calculated separately and is currently unrounded.
- Unknown values are `null`, except `band` and `vendor`, which use `"unknown"`.
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
- Each AP keeps the first 16 radio records produced, in their current
  order. Extra radios are left out with a warning counting the total
  omitted across all APs (§4.4). This bounds each AP record so an AP section
  page can always return at least one AP.
- The SSID summary keeps at most 50 non-null network groups (§4.3), with a
  counted warning for omitted groups and their clients (§4.4). The null
  group is always kept and does not count toward the limit. No clients are
  removed from `data.clients` by this summary limit.
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
| `clients` | list | — | `stat/sta` | WiFi clients (§4.2). |
| `no_ap_clients` | list | — | `stat/sta` | Up to 10 devices reported with no AP (§4.2). |
| `summary` | object | — | derived from `aps`, `clients`, and the full no-AP count | Counts and groupings (§4.3). |

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
| `radios` | list | — | `radio_table` joined to `radio_table_stats` by `name` | At most 16 entries, one per retained radio, below. |

`data.aps[].radios[]`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `band` | string | — | `radio` | |
| `channel` | int\|null | — | stats `channel`, else table `channel` if numeric | Channel in use. |
| `channel_setting` | string\|null | — | table `channel` | Configured value, e.g. `"auto"` or `"36"`. |
| `width_mhz` | int\|null | MHz | table `ht`, parsed | Channel width. |
| `tx_power_dbm` | int\|null | dBm | stats `tx_power` | AP transmit power; may be null. |
| `tx_power_mode` | string\|null | — | table `tx_power_mode` | `auto`, `high`, `medium`, `low`, or `custom`; may be null. |
| `channel_utilization_pct` | int\|null | % | stats `cu_total` | How busy the channel is, counting every network the AP can hear. |
| `self_rx_pct` | int\|null | % | stats `cu_self_rx` | Percent of all airtime this AP spends receiving; part of `channel_utilization_pct`. |
| `self_tx_pct` | int\|null | % | stats `cu_self_tx` | Percent of all airtime this AP spends transmitting; part of `channel_utilization_pct`. |
| `client_count` | int\|null | — | stats `num_sta` | |
| `satisfaction_pct` | int\|null | % | stats `satisfaction` | May be null. |
| `tx_retry_pct_cumulative` | float\|null | % | stats `tx_retries / (tx_packets + tx_retries)` × 100 | Since the counters reset. May be null, including when both counters are 0. |

`self_rx_pct` and `self_tx_pct` share `channel_utilization_pct`'s all-airtime
base; their sum did not exceed it in Jon's live check. A radio's channel
in use can differ from its configured setting, for example after the AP
moved off a DFS channel (one it must leave when it detects radar).
The module reports both and does not flag or interpret the difference.

### 4.2 Clients

Records with `is_wired` exactly true are excluded; a missing flag is treated
as wireless (unverified upstream assumption: U1, issue #16).

`data.clients[]` contains WiFi clients only. A readable dictionary with
`is_wired` not exactly true, a valid MAC, and `ap_mac` missing, null, or a
string that is empty or only whitespace goes into `data.no_ap_clients[]`
instead. Collection checks unreadable records first, then silently excludes
wired records, then rejects invalid MACs with the unreadable-client warning.
Within each list, the first record for a MAC is kept; the existing counted
duplicate-client warning counts the rest. If a MAC occurs in both lists,
the WiFi record wins: the no-AP record is dropped and counted in that same
warning, regardless of upstream order.

A present, non-blank `ap_mac` that matches no AP in `aps`, including a
non-blank value that is not a valid MAC, keeps the client in `clients[]`
with `ap_id` and `ap_name` null. The controller says it is associated with
an AP and reports radio data. The AP may be absent because its record was
unreadable, a duplicate, or not `type == "uap"` (possibly a console with
built-in WiFi). Dropping the client would hide a real WiFi client. No
warning is emitted for this case.

UniFi can report a device as wireless with no AP when it reaches the
network through another device rather than directly through a UniFi AP.
Examples include a speaker on its soundbar's own wireless link (such as a
bonded subwoofer or surround speaker), a device behind a wireless bridge,
extender, or repeater in client mode, or a computer connected by both cable
and WiFi. Housesitter does not tell which applies and has no radio readings
for these devices. Jon's UniFi OS live check saw such devices reported as
wireless (`is_wired` not true).

A client's `ap_id` is an AP in `aps` or `null`; when it is `null`,
`ap_name` is `null` too.

`data.clients[]`

| Field | Type | Unit | Source (`stat/sta`) | Meaning |
|-------|------|------|---------------------|---------|
| `id` | string | — | `mac` | **Identifier.** |
| `ip` | string\|null | — | `ip` | **Identifier.** |
| `name` | string\|null | — | `name`, else `hostname` | **Untrusted.** |
| `vendor` | string | — | `oui`, else `"unknown"` | Manufacturer looked up from the MAC prefix. Helps identify the device type. Never null or blank. **Untrusted.** |
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
| `tx_retry_pct_recent` | float\|null | % | `wifi_tx_retries_percentage` | UniFi's own retry figure; consistent with a recent window, whose length is undocumented. Absent for many clients (`null`); usually lower than the cumulative figure when both were present in Jon's live check. |
| `connected_s` | int\|null | s | `uptime` | Time since this association. |
| `idle_s` | int\|null | s | `idletime` | Time since last traffic. |
| `guest` | bool\|null | — | `is_guest` | |

In both client lists, `vendor` uses the upstream `oui` string,
or `"unknown"` when it is missing, null, not a string, or blank: empty,
only whitespace, or nothing left after the core removes control and
formatting characters. A literal upstream `"unknown"` reads the same.

`data.no_ap_clients[]`

| Field | Type | Unit | Source (`stat/sta`) | Meaning |
|-------|------|------|---------------------|---------|
| `id` | string | — | `mac` | **Identifier.** |
| `ip` | string\|null | — | `ip` | **Identifier.** |
| `name` | string\|null | — | `name`, else `hostname` | **Untrusted.** |
| `vendor` | string | — | `oui`, else `"unknown"` | Manufacturer; same rule as `clients[].vendor`. Never null or blank. **Untrusted.** |
| `private_mac` | bool | — | locally administered bit of `mac` | May appear with a different id later. |
| `ssid` | string\|null | — | `essid` | **Untrusted.** |
| `is_wired` | bool\|null | — | `is_wired` | Controller's flag as reported: false means wireless, null means missing. Never true, because wired records are excluded. |

No radio or rate fields are included: these records have none. Sort by
`name` (nulls last), then `id`, and list only the first 10. Records beyond
10 are dropped: they are not returned or saved, so `get` cannot recover them
later. The summary counts all no-AP records after de-duplication, including
the dropped ones.
Extra records produce the cap warning in §4.4; there is no separate warning
for having no AP.

The upstream field names, encodings, and units were checked on one UniFi
OS console running Network 10.6.x (U1, issue #16). Units and ranges were
plausible, and the `na`/`ng` → `n` mapping worked. Classic is not yet
checked. Missing fields stay `null`, except the `"unknown"` values in §4.0.

### 4.3 Summary

These are counts only, with no judgement. Groupings are lists, never maps
keyed by untrusted names (ARCHITECTURE §7). On the fixture house,
the sum of `aps[].client_count` equals `summary.client_count`.
On a real controller these counts come separately and can differ.

`data.summary`

| Field | Type | Unit | Source | Meaning |
|-------|------|------|--------|---------|
| `ap_count` | int | — | derived | APs in `aps`. |
| `ap_online_count` | int | — | derived | APs with `online` true. |
| `client_count` | int | — | derived | WiFi clients in `clients`. |
| `no_ap_client_count` | int | — | `stat/sta`, after de-duplication | Full no-AP count, including records beyond the 10 listed; never null. |
| `clients_by_band` | list | — | derived | `[{band, count}]` for every band value, including zeros. |
| `clients_by_ap` | list | — | derived | `[{ap_id, ap_name, count}]`. `ap_id` is an **identifier** and `ap_name` is **untrusted**. |
| `clients_by_ssid` | list | — | derived | `[{ssid, count}]`. At most 50 non-null groups, plus a null group if present. `ssid` is **untrusted**; distinct groups can display the same name (below). |

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
| `ssid` | string\|null | — | `clients[].ssid` | Network name from the raw `essid` grouping, then sanitised. **Untrusted.** Sorted by SSID, with a null group last if any clients have a null SSID. |
| `count` | int | — | derived | Clients in this raw SSID group; at least 1. |

An SSID is a WiFi network name. Collection groups by the raw SSID before
sanitising it. Distinct groups whose names become equal after
sanitising (stripped control or formatting characters, trimmed whitespace,
or truncation to the length limit) can therefore display the same name;
they stay separate. The client and summary SSID fields share the same 64-character
limit so their displayed names compare equally.

If there are more than 50 non-null groups, keep the 50 with the most
clients, breaking ties by ascending SSID string. Emit those groups in the
usual SSID order, with the null group last. The null group is always kept
and does not count toward 50. A warning counts the omitted groups and their
clients; the clients list still includes every client.

### 4.4 Order, compact form, paging

`get` re-validates saved records with the same value rules used at collection,
with the record-retention exception below.
An AP or client record that cannot be read or has no valid MAC `id`, an AP
with more than 16 saved radios, or a source with an invalid `kind`, `flavor`,
or `site` (§4.5 and §5), fails the whole command with
`upstream_unexpected`, "That saved snapshot is damaged."
Records are never moved or dropped, so paging offsets do not shift. A
snapshot saved before this change may still contain a no-AP client inside
`clients` with `ap_id` null; `get` returns it as saved. `get` applies the same
vendor rule as collection (§4.2), so older null or blank vendors return
`"unknown"`. Other invalid values in nullable fields become `null`; an invalid band becomes `"unknown"`.
Whole-snapshot `get` preserves and validates the saved `clients_by_ssid`
groups; it recomputes the other counts from the re-validated records, except
for the saved `no_ap_client_count`, which is preserved as described below.
The saved SSID summary must be a list of objects with exactly `ssid`
(string or null) and `count` (integer, not boolean, at least 1). It may have
at most 50 non-null entries and at most one null entry, which must be last.
The null entry's count must equal the number of re-validated clients with
a null SSID exactly. For each other displayed name, the sum of its entries'
counts must not exceed the number of re-validated clients with that name:
it can be lower, because a raw group that displays the same name may be the
one the 50-group limit left out. The shortfall, summed over all names
(including names not listed at all), must be zero without a recognised
saved SSID-summary warning. With one, the summary must list exactly 50
named groups, the warning's group count must not exceed its client count,
and the shortfall must equal its client count. Any failure gives the same
damaged-snapshot error. This preserves groups that display identically instead of merging
them on read.

Whole-snapshot `get` returns `no_ap_clients`, re-validated with the same
value rules. A missing key reads as an empty list, and a missing saved
`summary.no_ap_client_count` reads as 0, for snapshots saved before this
change. An unreadable record, a record without a valid MAC `id`, a record
with `is_wired` true (collection never lists one), a MAC listed twice or
also listed in `clients`, more than
10 records, or a count that is not an integer (booleans are not integers
here) gives the damaged-snapshot error. The saved count must equal the
list length unless a recognised saved no-AP cap warning is present. With
that warning, the list must contain exactly 10 records and the count must
equal 10 plus the warning's count; otherwise the snapshot is damaged.
The count is preserved, never recomputed from the capped list.
Section-only `get` (`aps` or `clients`) does not include `no_ap_clients`;
there is no `no_ap_clients` section.

Whole and section-only `get` also validate saved top-level `warnings`:
missing means an empty list; any value other than a list of strings gives
the damaged-snapshot error. Each string must match a known warning
template exactly, including its singular or plural wording, to be
repeated: unreadable AP/client records, duplicate AP/client MACs, the
radio, SSID, and no-AP limits below, the exact core warning
"TLS verification disabled by config (unifi_wifi.tls.insecure)", or
"N more warnings omitted". Counts are decimal integers from 1 to
`10**9`, without leading zeros; the no-AP cap warning uses the singular for 1
and the plural for counts of 2 or more, just like duplicate-MAC warnings.
Recognised warnings are regenerated from
shared templates and parsed counts, never copied from saved text.
Unrecognised strings are silently dropped. Saved warnings come first,
followed by warnings from re-validation, with duplicates removed.

Limit warnings use these exact texts (`N` and `M` are counts):

- "1 more client record with no AP was not listed"
- "N more client records with no AP were not listed"
- "1 radio record beyond the 16-per-AP limit was left out"
- "N radio records beyond the 16-per-AP limit were left out"
- "1 more SSID group with 1 client was not listed in the summary"
- "1 more SSID group with M clients was not listed in the summary"
- "N more SSID groups with M clients were not listed in the summary"

The radio warning counts omitted radios across all APs and is emitted only
when at least one was omitted. The SSID warning counts omitted groups and
their clients; singular `client` is used when the client count is 1.

- `aps` are ordered by `name`, then `id`. `clients` are ordered by
  `ap_name`, then `name`, then `id`. The order is deterministic and doesn't
  rank anything. Null names sort last at each name comparison.
- Compact form leaves `no_ap_clients` unchanged. With `identifiers: none`,
  its `id` and `ip` are omitted, as for clients.
- Compact form drops these client fields: `noise_dbm`, `idle_s`, `vendor`,
  `wifi_standard_raw`, `guest`, `ip`, `tx_retry_pct_recent`.
- If the result is still over budget, `clients` is truncated and `more`
  points at `get` with `section: "clients"` and the next `offset`.
- Whole-snapshot output keeps `summary`, `aps`, and `no_ap_clients` in full.
  Section-only `get` returns just that list, from `offset`; the runner pages it if needed.
- Because `summary`, `aps`, and `no_ap_clients` are not paged, a snapshot
  whose unpaged part cannot fit the output limit fails before saving, with the core's
  `upstream_unexpected` error "the result is too large even with paging:
  the part that cannot be paged is <N> bytes; the output limit is 90112
  bytes" (ARCHITECTURE §5). Nothing is saved. Measured on invented data:
  the limit is reached above about 75 APs with short names, 3 radios, and
  5 clients each, and above about 14 APs with 64-character emoji names and
  16 radios each. These limits assume an empty no-AP list. A full list of
  10 no-AP devices adds about 3.3 KB with 64-character plain names, and up to
  about 24 KB when every name, vendor, and SSID is 64 emoji, so the AP
  limits are lower in those houses. Every AP record is bounded (§4.0), so an `aps` section
  page always holds at least one AP, and the core's "one item in <section>
  is larger than the output limit" error is not expected for a snapshot
  this module saved.
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
| `unifi_os_*.json`, `classic_*.json` | Login responses, sysinfo, device, and sta for each flavour. The same invented house: 3 APs, 14 WiFi clients across all three bands, including a private-MAC phone and a 2.4 GHz-only device. Plus 1 wired record (excluded) and 1 no-AP client: a bridged speaker with no `ap_mac` and `is_wired: false`, listed in `no_ap_clients`; two WiFi clients, one with empty-string `oui` and one with no `oui` (both `"unknown"`); one radio with no satisfaction, retry counters, tx power, or tx power mode (nulls); one radio whose channel in use differs from its setting. Per-AP `num_sta` values match each AP's clients, so AP counts add up to the client count. |
| `edge_*.json` | Missing `radio_table_stats`, a null `signal`, an unknown `radio`, a `tx_packets` of 0 (no divide-by-zero), an AP that is offline. `edge_sta.json` adds no-AP records with empty-string and whitespace-only `ap_mac`, one with `is_wired` missing, and one sharing a MAC with a WiFi client (dropped as duplicate); null, non-string, and zero-width-only `oui` values (all `"unknown"`). Existing unmatched-AP clients are kept with `ap_id` null. |
| `hostile_*.json` | Names that are 5 KB long, names with control, bidi, or zero-width characters, an injection-style name, two clients with the same name, `x_authkey` and `x_passphrase` in device records, an `X-CSRF-Token` value, and a controller error message that tries to inject text. The planted secrets must be absent from output, saved files, and logs, and the error text must be absent from stderr. |
| `many_no_ap_sta.json` | 25 no-AP records, exercising the 10-record cap and its warning. |
| `large_sta.json` | 600 clients, to exercise compact form and paging. |

The tests check:

- Field-by-field normalisation for both flavours.
- No-AP ordering, de-duplication, cap warning, full summary count, identifier
  policy, and unchanged compact form.
- `get` accepts a snapshot saved without `no_ap_clients`, preserves older
  no-AP records in `clients`, applies vendor fallback, and validates the
  saved no-AP list, count, and cap warning together (§4.4).
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

- **U1. Settled 2026-11-10 by Jon for UniFi OS, Network 10.6.x** (issue #16).
  - Login and logout worked (§2.2); paged output worked (§4.4).
  - Field names, encodings, units, and ranges were checked; `na`/`ng` → `n` worked (§4.2).
  - Self RX/TX percentages use all airtime, as `cu_total` does (§4.1).
  - Recent retry figures were often absent and usually below cumulative figures when present; window length is undocumented (§4.2).
  - Radio satisfaction, cumulative retries, tx power, and power mode may be null (§4.1).
  - Channel in use may differ from its setting; both are reported without interpretation (§4.1).
- **U1-classic (still open).** Classic login/logout, including login-400
  handling, and the `auto` probe behaviour remain unverified (§2.2); no
  classic controller was available. Automatic detection stays **Planned**.
  The missing-`is_wired` assumption also remains unverified (§4.2); on the
  UniFi OS console, bridged devices were reported as wireless.
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
- **D1. Decided 2026-11-08 by Jon:** keep at most 16 radios per AP,
  warn with the total omitted, and reject saved APs over that limit (§4.0,
  §4.4).
- **D2. Decided 2026-11-08 by Jon:** keep raw SSID groups separate even
  when their displayed names match; whole `get` validates and preserves
  the saved groups (§4.3, §4.4).
- **D3. Decided 2026-11-08 by Jon:** whole and section-only `get` repeat
  recognised collection warnings from shared templates, including disabled
  TLS verification, with saved warnings first and duplicates removed (§4.4).
- **D4. Decided 2026-11-08 by Jon:** keep at most 50 non-null SSID groups,
  choosing by client count with SSID ties, always keep the null group, and
  warn with omitted group and client counts; keep every client (§4.3, §4.4).
- **D5. Decided 2026-11-10 by Jon:** WiFi clients only in `clients`;
  no-AP devices listed separately (at most 10, with the full count in the
  summary); vendor is never null (`"unknown"`). See DECISIONS D29 (§4.2, §4.4).
- **D6. Decided 2026-11-10 by Jon:** clients whose AP is not in `aps`
  stay in `clients` with `ap_id` and `ap_name` null, without a warning.
  See DECISIONS D29 (§4.2).
