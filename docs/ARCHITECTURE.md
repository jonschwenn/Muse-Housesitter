# Architecture

Status: **proposed** (no code yet). Every section describes the intended
design. Code that diverges from this document must update it.

## 1. The kernel of purpose

Muse can already run commands on a Raspberry Pi through the Linux gadget
SDK. What it lacks is **good eyes on the house**: reliable, well-labelled
measurements of the physical and network world around the Pi. Housesitter
supplies them:

- **Muse decides** when to look, what it means, and what to recommend.
- **Housesitter gathers** measurements from local systems, normalises them
  into documented fields with units, saves a local copy, and returns them.
- **A human acts.** Housesitter is read-only, and Muse's recommendations are
  applied by a person.

## 2. Pieces

```
 Muse (cloud VM)
   │  link.invoke "housesitter.unifi_wifi.snapshot" {"label": "before"}
   ▼
 musegadget service (SDK, on the Pi)                 /etc/musegadget/commands.d/
   │  exec ["/opt/housesitter/bin/housesitter", "invoke", "housesitter.unifi_wifi.snapshot"]
   │  params → stdin (JSON)            result ← stdout (JSON, ≤ 96 KiB)
   ▼
 housesitter core ── registry ── modules/unifi_wifi ── ctx.http (allow-listed GETs) ──▶ UniFi controller
   │                                     │
   │                                     └─ ctx.store.save() ──▶ ~/.local/share/housesitter/unifi_wifi/*.json (600)
   └─ envelope + size budget ──▶ stdout
```

### 2.1 How Muse reaches Housesitter

| Path | Use |
|------|-----|
| **Drop-in commands** (primary) | `housesitter install-commands` writes one SDK drop-in JSON file per command into `/etc/musegadget/commands.d/`. Muse sees them next to the built-ins, with their descriptions and parameters. After a `systemctl restart musegadget` they are callable. |
| **CLI via `system.run`** (fallback) | `housesitter invoke <command> --params '{...}'` prints the same envelope. This is useful before the drop-ins are installed, and for Muse to explore. |
| **`housesitter.describe`** | Returns the list of installed modules and each module's `SKILL.md`, so Muse gets its guidance from the device, versioned with the code. |
| **Paging via `get`** | Full results are saved locally. When a result is truncated, `more` gives the exact `get` call for the next page. `get` applies the same identifier policy, so paging returns exactly what the first page would. |

Housesitter does **not** use `musegadget send-user-msg` in v1. Muse
decides when to collect and when to look (rule 3), so pushing messages to
Muse would reverse that relationship. **[OPEN, D7]**

### 2.2 SDK constraints we design to

These come from the SDK's `commands.py`, `executor.py`, and README, as of
2026-11-04:

| Constraint (SDK source) | Consequence |
|------------|-------------|
| Command names match `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+`, at most 64 characters. `system.`, `file.`, `device.`, `link.` are reserved (`commands.py:59-61`). | Commands are named `housesitter.<module_id>.<command>`. Conformance checks the full length. |
| Drop-in keys are only `description`, `exec`, `required`, `optional`, `timeout_ms`. Each parameter spec must be **exactly** `{"type", "description"}`, with type one of `string`, `integer`, `number`, `boolean`, `object`, `array`. Undeclared params are silently dropped (`commands.py:62-160`). | The drop-in generator emits only `type` and `description`. Richer constraints on `Param` (enum, default, min/max, max length) are checked **by the core** and spelled out in the description text. Unknown params never reach us, so the core does not rely on rejecting them. |
| `timeout_ms` defaults to 30 s, maximum 600 s. | Each command declares its own timeout. The core sets a self-deadline 3 s shorter. Each HTTP request's timeout is the smaller of its configured timeout and the time left. |
| Params arrive as one JSON object on stdin. A JSON object on stdout is the result. **On a non-zero exit, stdout is discarded** and Muse gets only `"<name> exited with <n>: <last stderr line, ≤ 500 chars>"` (`executor.py:242-245`). | The error code must travel in the stderr line: `housesitter: <module>: <code>: <plain reason>`, kept under 400 characters. The stdout error object exists for the CLI path only. |
| The output limit is checked on the **JSON-escaped** text: `len(json.dumps(stdout_text)) - 2 ≤ 96 KiB`. Every `"` becomes `\"`, and non-ASCII becomes `\uXXXX`. Over the limit, Muse gets `{"output": <cut text>, "truncated": true}`, which is broken JSON (`executor.py:249-256, 388-397`). | The core serialises compactly (`separators=(",", ":")`) and measures with the SDK's formula. The budget is 88 KiB by that measure (§5). |
| The commands dir and its files are checked only when the service runs as root: they must be root-owned and not group- or world-writable. Symlinked dirs and files are refused, as are files over 64 KiB. The dir is read once, at service start. | `install-commands` writes real files (no symlinks), root-owned 644, each well under 64 KiB, and prints the restart step. Restarting kills any command Muse is running at that moment, so the tool warns about that. |
| Commands run as the run-as account, with no shell, and with an environment containing **only** `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG` (`executor.py:188-196`). Up to 4 commands run at once. | Config is found at `$HOME/.config/housesitter/config.json`. `$HOUSESITTER_CONFIG` works only on the CLI and dev path. Saved-file names and store writes must be safe under concurrency (§6). |
| The run-as account may have sudo ("if it can use sudo, so can Muse"). | Housesitter never needs sudo at run time, so either choice works. The trade-off is documented (§8, D11). |

## 3. Core components

| Component | Responsibility |
|-----------|----------------|
| `core/cli.py` | `housesitter invoke`, `list`, `describe`, `doctor`, `dropins`, `install-commands`, `config-check`, `conformance`. Parses arguments and calls the registry. Never prints secrets. |
| `core/registry.py` | Finds modules: built-in ones under `housesitter.modules`, plus third-party packages that expose the `housesitter.modules` entry point. Builds the command table, and refuses duplicate names. |
| `core/config.py` | Loads `~/.config/housesitter/config.json` (or `$HOUSESITTER_CONFIG`). Validates the core section and passes each module its own section, which the module's schema validates. |
| `core/secrets.py` | Reads a named secret from the file the config points to. Refuses a file that is group- or world-readable, or not owned by the current user. Returns the value without logging it. |
| `core/http.py` | The only network client, built on stdlib `http.client` and `ssl`. Each module gets one through `ctx.http(...)`, bound to one scheme, host, and port, and to the module's allow-list of `(method, path-regex)` pairs. Before any socket is opened, the path is normalised and **`fullmatch`ed**. Paths with `%`-escapes, `..`, or a query string are refused unless the entry allows them, and any other call raises `NotAllowed`. **Redirects are never followed**: a 3xx is returned as an `upstream_unexpected` error. TLS supports system verification, a CA file (with an optional `server_hostname` for IP hosts), or a pinned SHA-256 leaf fingerprint. The pin is checked right after the handshake, **before any request bytes are sent**. `insecure` must be explicit. Cookies and CSRF tokens are kept in memory only. Bodies are capped at 8 MiB, and request timeouts are bounded by `ctx.deadline`. |
| `core/store.py` | Per-module data directory (mode 700). `save(kind, obj, label)` writes atomically with mode 600 (temp file and `os.replace`). Names use UTC plus a random suffix (`snapshot_20261104T151500Z_3fa1.json`), so concurrent saves never collide. It returns a store-relative id. `list(kind, offset, limit)` returns every saved file, paged. `load(id)` accepts only ids that match the store's own naming pattern and resolve inside the module's directory. Retention is count-based and pruned after a save. `housesitter.retention` sets the default (500), `modules.<id>.retention` overrides it per module, and `0` keeps everything (D6). |
| `core/envelope.py` | Wraps every result (§4), enforces the size budget (§5), and sanitises untrusted strings. |
| `core/dropins.py` | Generates SDK drop-in JSON from the command table and validates it against the SDK rules in §2.2. `housesitter dropins --out DIR` runs **unprivileged**. `install-commands` then copies the files with `sudo install -o root -g root -m 644`. Housesitter itself (config, modules, third-party entry points) never runs as root. |
| `sdk.py` | The **public module API**. It is the only thing modules import from Housesitter. See [MODULES.md](MODULES.md). |

Built-in core commands, registered like module commands:

- `housesitter.describe`: modules, their commands, and each module's `SKILL.md` text.
- `housesitter.doctor`: config validity, secret file modes, data directory permissions, and an optional connectivity check per module (`Module.check(ctx)`). Read-only.

## 4. Output envelope

Every command's stdout is exactly one JSON object:

```json
{
  "ok": true,
  "schema": "housesitter.unifi_wifi.snapshot/1",
  "module": "unifi_wifi",
  "module_version": "0.1.0",
  "housesitter_version": "0.1.0",
  "collected_at": "2026-11-04T15:15:00Z",
  "source": { "kind": "unifi", "flavor": "unifi_os", "controller_version": "9.0.114", "site": "default" },
  "label": "before: IoT 5 GHz on",
  "data": { "…": "module-defined, documented in docs/modules/<id>.md" },
  "warnings": ["TLS verification disabled by config (unifi_wifi.tls.insecure)"],
  "truncated": false,
  "saved": "snapshot_20261104T151500Z_3fa1",
  "more": null
}
```

- `schema` is versioned. A breaking change to `data` bumps it and is
  recorded in the module spec.
- `collected_at` is the time the data was observed, not the time it was
  returned. If the upstream system reports its own observation time, the
  module puts it in `source`.
- `warnings` are plain-language facts about data quality or configuration.
  They are not analysis.
- On failure the process exits non-zero. Stderr's **last** line is
  `housesitter: <module>: <code>: <plain reason>`, under 400 characters. It
  contains no secrets, no URLs with credentials, no stack traces, and **no
  upstream-supplied text** (e.g. a controller's error message), because
  that text is untrusted. This line is all Muse sees through the SDK
  (§2.2). Stdout also gets `{"ok": false, "error": {"code": "…", "message":
  "…"}}` for CLI callers.
- Login failures (`upstream_auth`) are never retried. Every login attempt is
  logged by the controller, and repeated failures can lock the account.

Stable error codes: `config_invalid`, `secret_unreadable`, `upstream_auth`,
`upstream_unreachable`, `upstream_tls`, `upstream_unexpected`, `not_allowed`,
`timeout`, `bad_params`, `not_found`.

## 5. Size budget and paging

- The envelope is serialised compactly and measured with the SDK's own
  formula, `len(json.dumps(text)) - 2`. That measure must be ≤ **88 KiB**,
  leaving headroom under the SDK's 96 KiB cut. A conformance test asserts
  the measure on the large and hostile fixtures, the latter heavy in
  non-ASCII.
- If `data` would exceed it, the core asks the module for a **compact
  form** (`Result.compact`). If that is still too big, the core truncates
  list fields from the end, in the module's declared order. It sets
  `truncated: true` and fills `more` with exactly how to fetch the rest,
  e.g. `{"command": "housesitter.unifi_wifi.get", "params": {"id": "…",
  "section": "clients", "offset": 120}}`. `get` pages its own output the
  same way.
- The full result is always in the saved file, so truncation never loses
  data, only defers it.

## 6. Files, config, and secrets

Everything lives under the SDK's run-as account:

| Path | Mode | Contents |
|------|------|----------|
| `~/.config/housesitter/config.json` | 600 | Core and per-module config. No secrets. |
| `~/.config/housesitter/secrets/<name>` | 600, dir 700 | One secret per file, e.g. `unifi_password`. |
| `~/.local/share/housesitter/<module_id>/` | 700 | Saved results, 600 each. |
| `/etc/musegadget/commands.d/housesitter.*.json` | root-owned, 644 | Generated drop-ins. |
| `/opt/housesitter/` | root-owned | Install venv (**[OPEN, D4]**). |

The config is **JSON**, because Python 3.9 has no stdlib TOML reader and
JSON is easy for Muse to read and edit (D3). Example:

```json
{
  "housesitter": { "identifiers": "full", "retention": 500 },
  "modules": {
    "unifi_wifi": {
      "enabled": true,
      "retention": 1000,
      "host": "192.0.2.1",
      "site": "default",
      "flavor": "auto",
      "auth": { "username": "housesitter-readonly", "password_secret": "unifi_password" },
      "tls": { "fingerprint_sha256": "AB:CD:…" }
    }
  }
}
```

## 7. Identifiers and privacy

**Housesitter is a personal tool.** It is meant to be run by the owner, on
the owner's own network, for the owner's own Muse (D2). It is not built for
sharing reports, for multiple households, or for running on a network you
don't control.

Results go to Muse, which runs in the cloud. That is the point, and it
means that what goes into an envelope leaves the house.

- **Real identifiers by default.** MACs and IPs are returned as they are, so
  Muse can match devices across snapshots and against what the owner sees in
  the UniFi app. `housesitter.identifiers` is `full` (default) or `none`. With
  `none`, fields marked as identifiers are left out of returned output. No
  hashing.
- Saved local files always hold full values. `get` applies the same policy
  as `snapshot`, so paging never returns more than the first page would.
- Secrets are a different matter from identifiers: they never appear in
  output under any setting (AGENTS rule 6).
- Names, SSIDs, and AP names are returned, because Muse needs them to talk
  about the house. They are sanitised (rule 8):
  - Unicode categories `Cc` and `Cf` (control, bidi, zero-width) are
    stripped.
  - Whitespace is collapsed.
  - Values are capped at 64 characters.
  - Untrusted text is **never used as a JSON object key**. Groupings are
    lists of `{id, name, count}`, so names that collide or are null can't
    merge or spoof entries.
- Owner-supplied `label` text is capped at 200 characters and sanitised the
  same way.
- Modules declare which fields are identifiers (`Field(..., identifier=True)`),
  and the core applies the policy. Modules never strip identifiers on their own.

## 8. Threat model (short form)

| Threat | Stance |
|--------|--------|
| Muse can read the UniFi password file, because it runs commands as the same account. | Accepted, and stated plainly. File modes protect the secret from *other* local users and from accidental leaks. The real control is a **View Only** UniFi account (or API key, D10) that is unique to Housesitter. |
| The SDK's run-as account has sudo, so Muse has root. | The owner decides this. It is outside Housesitter's control. README explains the trade-off, and `doctor` reports (as information, not a failure) whether the account can sudo. |
| The allow-list is bypassed by an HTTP redirect, or by a path trick (`stat/device-basic`, `%2e%2e`, `?`). | No redirects. `fullmatch` on the normalised path. Escapes and queries refused. Conformance tests each case. |
| A module writes to upstream. | Prevented structurally by the HTTP allow-list. A conformance test proves it, and the module's threat review checks it. |
| Upstream returns secrets, e.g. WLAN passphrases in `rest/wlanconf` or `x_*` keys in device records. | Normalisation is an **allow-list of fields**, never "copy and delete." Optional raw dumps strip `x_*` keys and known secret fields, and are off by default. |
| Prompt injection via a device name like `"ignore previous instructions…"`. | Names are typed data fields, sanitised and capped. SKILL.md tells Muse they are untrusted. Names never appear in `description` strings. |
| Self-signed controller certificate. | Pin the SHA-256 fingerprint (preferred) or a CA file. `insecure: true` works, but adds a warning to every envelope. |
| Tampered drop-in files. | The SDK refuses non-root-owned or writable files. `install-commands` writes root-owned 644 files and `doctor` checks them. |
| Anyone who can reach the dashboard port sees device names, MACs, and IPs (D14). | Accepted for a personal tool, and stated in README. It binds to `127.0.0.1` by default, and the owner chooses a LAN address on purpose. |
| A malicious website reads the dashboard through the owner's browser by DNS rebinding. | The dashboard server checks the `Host` header against an allow-list (DASHBOARD.md §5). |
| Hostile device names reach the dashboard as HTML. | The page inserts text only, and a CSP with Trusted Types blocks inline script and string-to-HTML assignment. A hostile fixture tests every text field. |
| Saved data on a lost SD card. | Out of scope for v1 (no encryption at rest). The data dir is 700, and retention bounds the volume. |

## 9. What is deliberately not here

The inventory editor, the what-if throughput model, the exception rules,
and the change-log engine from the previous draft
(`.context/SPEC-v0-hub-draft.md`) are dropped. They are analysis, and
analysis is Muse's job. Any of them can return later as a module or a
SKILL.md recipe if real use calls for it.

The web hub came back in a narrower form: a **read-only dashboard** that
presents saved results without analysing them (D14). See
[`DASHBOARD.md`](DASHBOARD.md).
