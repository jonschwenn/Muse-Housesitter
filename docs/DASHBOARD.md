# Dashboard

Status: **contract defined; front end and server built.** This document is
the handoff between the backend (the server and the data it serves) and the
front end (the pages in `web/`). Each side builds against it without waiting
for the other.

## 1. What it is

A read-only dashboard for people to see the house at a glance in a browser,
served by Housesitter on the Pi (default port 8080). Later, the same view can
drive an e-ink display. It shows what Muse last collected. Muse decides when
to collect (D14), so the page changes only when Muse runs a module, or when
the owner runs one by hand.

The dashboard presents data. It does not analyse it: no scores, verdicts, or
advice computed by Housesitter (AGENTS rule 2). Showing the change between
the latest and previous snapshot (for example "−58 → −54 dBm") is
presentation, and is fine. Muse's own analysis can appear in the
`muse_note` panel (Planned, §4.2).

## 2. Pieces

```
browser ── GET / , /eink.html, /*.js, /*.css ──▶ housesitter serve
        ── GET /api/dashboard ──────────────────▶   reads saved results only
                                                    never contacts UniFi
                                                    never writes anything
```

- **Server (backend):** `housesitter serve`, a stdlib HTTP server.
  It answers GET and HEAD only, serves `web/` and `/api/dashboard`, binds to
  the address and port in config, and sends the headers in §5.
- **Dev server (exists):** `scripts/dev_dashboard.py` serves `web/` and
  answers `/api/dashboard` from `tests/fixtures/dashboard/<name>.json`. A
  page can choose the fixture with `?fixture=<name>`. This is what the front
  end is built and tested against. Regenerate the fixtures with
  `scripts/make_dashboard_fixtures.py`, never by hand.
- **Front end (exists):** `web/index.html`, `web/eink.html`,
  `web/dashboard.js`, `web/dashboard.css`, and `web/eink.css`, with design
  notes in `web/DESIGN.md`. No build step or external assets. Use
  `housesitter serve` for real saved results and `scripts/dev_dashboard.py`
  for front-end work against fixtures.

## 3. Page contract (front end)

- **Files:** `web/index.html` (the dashboard), `web/eink.html` (the e-ink
  view), and the CSS, JS, and SVG next to them. Both pages live at the
  web root, because the data URL below is relative.
- **No build step:** no npm, bundlers, frameworks loaded from a CDN, web
  fonts from the internet, or third-party code. What is in `web/` is what
  the Pi serves.
- **Data:** each page fetches `api/dashboard` (a relative URL) with the
  page's own query string appended, i.e. `"api/dashboard" + location.search`.
  The real server ignores the query string, and the dev server uses it to
  pick a fixture.
- **Scripts in files only.** The Content-Security-Policy (§5) blocks inline
  `<script>` and `on…=` attributes. Inline `style` attributes are allowed.
  It also requires Trusted Types. In browsers that enforce it, these
  **throw**:
  - assigning a string to `innerHTML` or `outerHTML`, including the common
    `el.innerHTML = ""` to clear an element (use `el.replaceChildren()`)
  - `insertAdjacentHTML`, `document.write`, and `DOMParser.parseFromString`
  - `setTimeout` or `setInterval` with a string

  Build the DOM with `createElement`, `textContent`, `setAttribute` (but
  never `on…` attributes), and `replaceChildren()`. **Test in a
  Chromium-based browser**, which enforces Trusted Types. Other browsers may
  ignore it and hide a violation. `form-action 'none'` also blocks forms, so
  make a reload control a `<button>` with a script handler.
- **Untrusted text.** Every string from the view model is untrusted
  (AGENTS rule 8): device names, AP names, SSIDs, vendors, labels,
  warnings, `house_label`, `last_error.message`, and the Muse note. Insert
  it as text, never as HTML. Never use it as an object key without a
  `Map` (names like `__proto__` exist), and never use it in a URL or a
  `style` value. `tests/fixtures/dashboard/hostile.json` exists to prove
  this.
- **States every page handles**, one fixture each:

  | Fixture | State |
  |---------|-------|
  | `typical` | A normal house with 60 snapshots (so `recent` holds 50 of them), and the before/after speaker story in `previous` and `latest`. |
  | `empty` | The module has never run (`status: never`, `unifi_wifi: null`). |
  | `error` | **Planned:** the last attempt failed; last good data is still shown. |
  | `failed` | No readable data (`status: error`, `unifi_wifi: null`); failed-attempt reporting is **Planned**. |
  | `large` | 8 APs, one of them offline (no radio stats), and 600 clients. |
  | `hostile` | Markup, script, CSS, URL, and prototype-key injection in every text field, including in `previous`. |
  | `edge` | Every nullable field is `null` somewhere (in `previous` too), a client whose band is `unknown`, a client with no AP, an AP without radio stats, and controller counts that disagree with the client list. |

- **Data age:** show how old the data is, so old data is obvious. Compute it
  as `generated_at − collected_at`, not from the browser's clock. That stays
  right when the Pi's and the browser's clocks differ, and keeps fixture
  screenshots stable.
- **Comparing `previous` and `latest`:** match clients by `id`, and APs by
  `id`. Clients only in `latest` are new, and clients only in `previous` are
  gone. Don't match by name: names can repeat. A client with
  `private_mac: true` can come back with a different `id`, so a page may
  note that "new" and "gone" private-address devices could be the same
  device.
- **`index.html`:** works from a phone (360 px wide) to a desktop. Readable
  in light and dark mode. Usable with a keyboard. Doesn't depend on colour
  alone; for example, a weak signal needs a label or icon, not only red.
- **`eink.html`:** a fixed **800 × 480** layout. Black and white only (no
  greys, no anti-aliasing tricks), large type, no animation, and nothing
  that relies on hover. It must make sense as a still image, because it will
  be rendered to PNG for e-ink panels later. It can't list 600 clients, so it
  shows counts, the APs, and at most a handful of clients. Choosing which
  clients is up to the design, for example the weakest signals, sorted by
  `signal_dbm` and labelled with the reference ranges below. Add a plain
  "+ N more" when it trims.
- **Refreshing:** pages may re-fetch `api/dashboard` (once a minute is
  plenty), or offer a reload button. Fetching from the Pi is fine. The page
  must never trigger collection, and the server offers no way to.
- **When loading fails:** if the request fails, the reply isn't JSON, or
  `schema` isn't `housesitter.dashboard/1`, say so plainly ("Can't load
  the dashboard data" or "This page doesn't understand this data version")
  and keep showing the last data that did load, with its age.
- **Disabled modules:** a disabled module has no entry in `modules`, and
  its key (e.g. `unifi_wifi`) is `null`. Show nothing for it, not an
  error.
- **Units and meanings:** every AP, radio, and client field is documented
  with its unit in
  [`docs/modules/unifi_wifi.md`](modules/unifi_wifi.md) §4. Show units on
  screen (dBm, Mbps, MHz, %).
- **Display precision:** pages round for display only: one decimal for retry
  percentages; whole numbers for other percentages unless fractional; whole
  dBm; negotiated rates in whole Mbps once they round to 10 or more (so
  9.96 shows "10"), and one decimal below; whole uplink Mbps and counts, with
  no thousands separators. Channel, width, durations and times are unchanged.
  Reference labels use the unrounded value. Near a range boundary the page
  shows up to two more decimals so the number and label agree (for example,
  9.96 % stays "9.96 %", normal). `/api/dashboard` is never rounded.
- **Reference ranges (labels, not verdicts):** a page may label values only
  with these rules of thumb. The `unifi_wifi` SKILL.md (Planned) must give
  Muse the same ranges, so people and Muse read the numbers the same way.
  The page doesn't invent its own cut-offs, and it never says what to do
  about a value. That is Muse's job (AGENTS rule 2).

  | Value | Ranges |
  |-------|--------|
  | `signal_dbm` | −60 or higher: strong · −60 to −70: good · −70 to −75: weak · below −75: very weak |
  | `channel_utilization_pct` | under 40: light · 40–70: busy · over 70: congested |
  | `tx_retry_pct_cumulative` | under 10: normal · 10–20: elevated · over 20: high |
  | `satisfaction_pct` | UniFi's own score. Show it as it is, without a label. |
- **Radio terms in plain language:** a short explanation is available for
  dBm, band, channel, channel width, channel utilization, negotiated rate,
  retries, and satisfaction (a tooltip, a footnote, or a help panel).

## 4. Data contract: `GET /api/dashboard`

Schema id `housesitter.dashboard/1`. A breaking change bumps the version and
updates this document, `tests/test_dashboard_contract.py`, and
`scripts/make_dashboard_fixtures.py` in the same PR.

With `housesitter.identifiers: "none"`, every identifier field keeps its
key with a `null` value: client `id`, `ip`, and `ap_id`, AP `id`, and
`summary.clients_by_ap[].ap_id`. The key sets stay exactly as below, but
three rules in §4.4 don't hold: client ids aren't unique, a client's
`ap_id` is `null` even when `ap_name` names its AP, and `clients_by_ap`
can't be matched to clients by `ap_id`. Pages can't match clients or APs
between `previous` and `latest` in this mode (D24).

Conventions:
- Times are ISO 8601 UTC (`2026-11-06T15:00:00Z`).
- Unknown values are `null`.
- Units are in field names, as in the module spec.

### 4.1 Top level

`view`

| Field | Type | Meaning |
|-------|------|---------|
| `schema` | string | `"housesitter.dashboard/1"` |
| `generated_at` | string | When the server built this response. |
| `housesitter_version` | string | |
| `house_label` | string\|null | Owner-chosen name from config. **Untrusted.** |
| `modules` | list | One entry per supported enabled module (v1: `unifi_wifi`, §4.3). |
| `muse_note` | object\|null | **Planned:** Muse's latest written summary (§4.2). |
| `unifi_wifi` | object\|null | `null` if the module is disabled or has no readable saved snapshot (§4.4). |

### 4.2 Muse note (Planned)

`muse_note`

| Field | Type | Meaning |
|-------|------|---------|
| `text` | string | Plain text, at most 2,000 characters. **Untrusted.** No Markdown rendering in v1. |
| `written_at` | string | |

How Muse writes it (a command that writes only Housesitter's own data) is
**Planned**, and will need its own spec. Designs should leave room for it.

### 4.3 Modules

`modules[]`

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | e.g. `unifi_wifi` |
| `title` | string | e.g. "UniFi WiFi radio metrics" |
| `version` | string | |
| `status` | string | `ok` (a readable snapshot exists), `never` (no saved snapshots), `error` (saved snapshots exist but none are readable). |
| `last_success_at` | string\|null | Latest readable snapshot's `collected_at`, or `null`. |
| `last_attempt_at` | string\|null | Equals `last_success_at` in v1. |
| `last_error` | object\|null | `{code, message}`. `code` is one of the stable error codes in ARCHITECTURE §4. `message` is plain text. |

In v1, failed collection attempts are not recorded. Reporting their status
and `last_error` is **Planned**, requiring collector attempt recording in a
follow-up issue (D23). If saved snapshots exist but none are readable,
`last_error` is `{code: "upstream_unexpected", message: "Saved snapshots could
not be read."}`, both timestamps are `null`, and `unifi_wifi` is `null`.
Otherwise `last_error` is `null`.

### 4.4 UniFi WiFi

`unifi_wifi`

| Field | Type | Meaning |
|-------|------|---------|
| `snapshots` | object | `{total, recent}`. `recent` lists up to 50 history entries, newest first. |
| `latest` | object | The newest snapshot, in full. |
| `previous` | object\|null | The one before it, in full, so the page can show changes. |

These always hold (tested in `tests/test_dashboard_contract.py`):
- `unifi_wifi` is `null` exactly when there is no readable saved snapshot (status
  `never`, or `error` with no readable data), or when the module is
  disabled. Otherwise `latest` is set.
- `recent` is strictly newest first, and `recent[0]` is `latest`.
- `previous` is set exactly when `total ≥ 2`, and it is `recent[1]`.
- Within a snapshot, client ids are unique.
- Every `summary` count is derived from `aps` and `clients`, so it always
  agrees with them.
- A client's `ap_id` is either an AP in `aps` or `null` (UniFi couldn't
  place it). In the `null` case, `ap_name` is `null` too.

These do **not** always hold. A page must not rely on them:
- AP and radio `client_count` are the controller's own counts, read
  separately from the client list, so they can differ from `clients`. Use
  `summary` or count `clients` when the numbers must agree. Show the
  controller's count as the controller's.
- A radio's stats, including `channel` and `client_count`, can be `null`
  when UniFi doesn't report them, for example for an offline AP. A client's
  `channel` can then be `null` too.

The `edge` fixture breaks both of these on purpose.

`unifi_wifi.snapshots.recent[]`

| Field | Type | Meaning |
|-------|------|---------|
| `id` | string | Store id of the snapshot. |
| `collected_at` | string | |
| `label` | string\|null | Free text given when the snapshot was taken. **Untrusted.** |
| `ap_count` | int | |
| `client_count` | int | |
| `clients_by_band` | list | `[{band, count}]` |

`unifi_wifi.latest` and `unifi_wifi.previous` each have `id`,
`collected_at`, `label`, `source`, `warnings` (list of strings, **untrusted**),
`summary`, `aps`, and `clients`. `source`, `summary`, `aps[]`,
`aps[].radios[]`, and `clients[]` are **exactly** the objects defined in
[`docs/modules/unifi_wifi.md`](modules/unifi_wifi.md) §4, with the same
fields, units, and meanings. The default identifier policy is `full` (D2).

The view reads only saved `unifi_wifi` snapshots, through the module's own
`get` validation and the same output policy as `invoke`: sanitising text,
applying the identifier setting, and checking declared fields. Its Context
cannot reach the network. Unreadable, damaged, and old-schema files are
skipped, with a stderr line naming the snapshot id. Results are cached in
memory per saved file; nothing is written. `recent` holds the newest 50
readable snapshots; `total` counts readable snapshots after deduplication.
When files share a `collected_at`, only the newest file is shown and counted.
If config or the store becomes unusable, the API returns 503 with
`{"error": "dashboard data unavailable"}`.

## 5. Server behaviour

`housesitter serve` has no flags. It runs in the foreground; Ctrl-C stops
it with exit 0. It is a CLI subcommand only: no Muse command or drop-in is
generated. The installer can install an optional hardened systemd unit with
`--with-dashboard`. It runs as the SDK run-as account, read-only, without
access to the secrets directory. It is not started during installation. See
[README Setup](../README.md#setup) for installation and enabling it at boot.

All settings in `housesitter.dashboard` are optional and validated by
`housesitter config-check`:

| Key | Type | Default | Meaning |
|-----|------|---------|---------|
| `bind` | string | `"127.0.0.1"` | IPv4 or IPv6 literal to listen on; `0.0.0.0` and `::` are allowed. |
| `port` | integer | `8080` | Listening port, 1–65535. |
| `allowed_hosts` | list of strings | `[]` | Up to 32 hostnames or IP literals accepted by the Host check. Use bare IPv6 literals in config. |
| `house_label` | string or null | `null` | Owner-chosen label shown on the page, at most 64 characters; treated as untrusted text. |

`bind`, `port`, and `allowed_hosts` are read at startup; restart to change
them. `house_label` and the rest of config, including identifiers and enabled
modules, are re-read on every `/api/dashboard` request.

Startup prints `serving the dashboard on http://<bind>:<port>/ (read-only)`
(with brackets around IPv6). A non-loopback bind also prints a reminder
that anyone who can reach the port sees device names, MACs, and IPs.
Startup errors print one stderr line and exit 2: invalid config, missing
web files, address unavailable, port in use, or permission denied (ports
below 1024 need privileges). For example:

```text
housesitter: serve: cannot bind 127.0.0.1:8080: address already in use; stop the other server or change housesitter.dashboard.port
```

- GET and HEAD only. Any other method returns 405.
- Never contacts an upstream system, never triggers collection, never
  writes. It opens files read-only.
- Binds to `housesitter.dashboard.bind` and `housesitter.dashboard.port` from
  config. The default is `127.0.0.1:8080`, and the owner sets a LAN address
  to reach it from other devices. If Muse's earlier demo page still uses
  port 8080 on the Pi, stop it or choose another port. When binding to a LAN
  address or `0.0.0.0`, list every name or IP you'll type into the browser
  in `allowed_hosts` (next point).
- **Host check:** a request whose `Host` header isn't the bind address,
  `localhost`, `127.0.0.1`, `[::1]`, or a name in
  `housesitter.dashboard.allowed_hosts` gets 421. Without this check, a
  malicious website could use DNS rebinding to read the dashboard, even one
  bound to `127.0.0.1`, through a browser on the Pi or the owner's laptop.
  Ports are ignored in the comparison, and IPv6 matches with or without
  brackets. Missing or duplicate Host headers also get 421.
- Static files are read into memory at startup; restart to pick up changes.
  Only regular, non-dot `.html`, `.css`, `.js`, `.svg`, `.png`, and `.ico`
  files directly in `web/` are served. `/` maps to `index.html`. Everything
  else gets 404, including `DESIGN.md`, symlinks, dotfiles, directories, and
  paths containing `%`, `..`, a backslash, or NUL. No directory listings.
  `web/` stays at the repo root and ships as package data in `housesitter.web`,
  so `serve` works from a pip install.
- No login in v1, because it is read-only. **Anyone who can reach the port
  sees everything on the dashboard, including MACs and IPs.** The README must
  say so.
- Response headers on every response: `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, and
  `Content-Security-Policy: default-src 'self'; img-src 'self' data:;
  style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self';
  object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors
  'none'; require-trusted-types-for 'script'`.
  Also sends `Referrer-Policy: no-referrer`, including on errors. Error
  bodies are short fixed text.
  `scripts/dev_dashboard.py` sends the same CSP, `Cache-Control`, and
  `X-Content-Type-Options`, and does the same Host, listing, and dotfile checks.
- The owner runs it as the SDK's run-as account, with read-only access to
  the data directory. The server never writes regardless of permissions.
- Request logs go to stderr, with control characters escaped, URL userinfo
  removed, and lines capped at 500 characters.

## 6. Evidence

Front-end PRs carry rendered before/after screenshots (REVIEW.md,
"Evidence"). Each page is captured for each fixture, with `index.html` at
phone and desktop widths and `eink.html` at 800 × 480. The screenshot tooling
exists (`scripts/ui_evidence.py`, tested in `tests/test_ui_evidence.py`)
and runs in CI. The evidence-publish workflow publishes the screenshots
(D16).

## 7. Design bakeoff (complete)

Three options were built against this contract and the fixtures:
- A, "control room": a detailed view.
- B, "house at a glance": an e-ink-first view.
- C, "change-first": leads with what changed since the last snapshot.

Jon chose C's overall design, A's per-radio and per-client detail, and B's
e-ink composition, with the Slate / Blue palette in light and dark modes
(D17). The A/B/C branches were not merged. The front end in `web/` brings
these choices together.

Jon expects to tweak specific aspects over time for actionability and
quality, each through a normal PR with screenshot evidence. See
[DECISIONS D17](DECISIONS.md#d17-dashboard-design-bakeoff-outcome-2026-11-07-jon).
