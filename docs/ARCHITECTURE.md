# Architecture

Status: the core API, registry, config, Context, HTTP client, secrets,
store, runner, envelopes, drop-ins, and CLI are implemented. No data module
is implemented yet. The conformance suite and `housesitter conformance`
CLI command are **Planned (12c)**. Module examples below illustrate the
contract; they are not installed commands.

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
| **Paging via `get`** | A module can save full results and declare a paging command (often `get`). When output is truncated, `more` gives the next call. Paging handlers run through the same identifier policy. |

Housesitter does **not** use `musegadget send-user-msg` in v1. Muse
decides when to collect and when to look (rule 3), so pushing messages to
Muse would reverse that relationship. **[OPEN, D7]**

### 2.2 SDK constraints we design to

These come from the SDK's `commands.py`, `executor.py`, and README, as of
2026-11-04:

| Constraint (SDK source) | Consequence |
|------------|-------------|
| Command names match `[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+`, at most 64 characters. `system.`, `file.`, `device.`, `link.` are reserved (`commands.py:59-61`). | Commands are named `housesitter.<module_id>.<command>`. The registry and drop-in validator check the full length. |
| Drop-in keys are only `description`, `exec`, `required`, `optional`, `timeout_ms`. Each parameter spec must be **exactly** `{"type", "description"}`, with type one of `string`, `integer`, `number`, `boolean`, `object`, `array`. Undeclared params are silently dropped (`commands.py:62-160`). | The drop-in generator emits only `type` and `description`. The runner validates incoming values against richer `Param` constraints (enum, default, min/max, max length). Definitions are checked at construction, and `drop_in_description()` spells out constraints in text. The runner rejects unknown params that reach it, including through the CLI. |
| `timeout_ms` defaults to 30 s, maximum 600 s. | Each command declares its own timeout. The runner sets a self-deadline 3 s shorter. Each HTTP request's timeout is the smaller of its configured timeout and the time left. |
| Params arrive as one JSON object on stdin. A JSON object on stdout is the result. **On a non-zero exit, stdout is discarded** and Muse gets only `"<name> exited with <n>: <last stderr line, ≤ 500 chars>"` (`executor.py:242-245`). | The error code must travel in the stderr line: `housesitter: <module>: <code>: <plain reason>`, kept under 400 characters. The stdout error object exists for the CLI path only. |
| The output limit is checked on the **JSON-escaped** text: `len(json.dumps(stdout_text)) - 2 ≤ 96 KiB`. Every `"` becomes `\"`, and non-ASCII becomes `\uXXXX`. Over the limit, Muse gets `{"output": <cut text>, "truncated": true}`, which is broken JSON (`executor.py:249-256, 388-397`). | The core serialises compactly (`separators=(",", ":")`) and measures with the SDK's formula. The budget is 88 KiB by that measure (§5). |
| The commands dir and its files are checked only when the service runs as root: they must be root-owned and not group- or world-writable. Symlinked dirs and files are refused, as are files over 64 KiB. The dir is read once, at service start. | `install-commands` writes real files (no symlinks), root-owned 644, each well under 64 KiB, and prints the restart step. Restarting kills any command Muse is running at that moment, so the tool warns about that. |
| Commands run as the run-as account, with no shell, and with an environment containing **only** `HOME`, `USER`, `LOGNAME`, `PATH`, `LANG` (`executor.py:188-196`). Up to 4 commands run at once. | Config is found at `$HOME/.config/housesitter/config.json`. `$HOUSESITTER_CONFIG` works only on the CLI and dev path. Saved-file names and store writes must be safe under concurrency (§6). |
| The run-as account may have sudo ("if it can use sudo, so can Muse"). | Housesitter never needs sudo at run time, so either choice works. The trade-off is documented (§8, D11). |

## 3. Core components

| Component | Responsibility |
|-----------|----------------|
| `core/cli.py` | Implements `--version`, `invoke`, `list`, `describe`, `doctor`, `dropins`, `install-commands`, and `config-check` (§3.1). Never prints secrets. `conformance` is **Planned (12c)**. |
| `core/registry.py` | Discovers built-in subpackages of `housesitter.modules`, each defining exactly one `Module` subclass with an id matching the package name. Third-party entry points in the `housesitter.modules` group must load a `Module` subclass whose id equals the entry-point name. Validates declarations (MODULES §3), records third-party status, sorts modules by id, and builds a read-only command table. The id `housesitter` is reserved for core commands. Duplicate ids or command names, invalid declarations, or any import/load failure stop the registry. |
| `core/config.py` | Loads `~/.config/housesitter/config.json` (or `$HOUSESITTER_CONFIG`), capped at 1 MiB. A missing file means defaults (`identifiers="full"`, retention 500) with no modules enabled. Rejects unknown top-level/core keys, unknown modules, and unknown keys in enabled module sections; validates values and applies defaults using each module's `Setting` declarations. A present module section defaults to enabled; disabled sections check only `enabled` and `retention`, ignoring other keys and values. Returns read-only config; the secrets directory is `<config dir>/secrets`. Errors name the key or file and problem, never the offending value. |
| `core/context.py` | Implements `sdk.Context`: deeply read-only supplied config, secret reading, logging that redacts secrets read through the Context, UTC clock, remaining monotonic deadline, and allow-listed HTTP sessions. Passes the deadline on every request, maps 401/403 to `upstream_auth`, and records insecure-TLS warnings. The runner injects the module's real store; without one, using store methods raises `NotImplementedError`. |
| `core/secrets.py` | Reads a named secret from the file the config points to. Refuses a file with any group or world permission bits, or not owned by the current user. Symlinks are refused (`O_NOFOLLOW`, plus `O_NONBLOCK` so a FIFO cannot hang the open); every check — regular file, mode bits, owner — runs on the open file descriptor, so there is no stat→open window. Files over 64 KiB are refused. Exactly one trailing `\n` or `\r\n` is stripped. Returns the value without logging it. |
| `core/http.py` | The only network client, built on stdlib `http.client` and `ssl`. Each module gets one through `ctx.http(...)`, bound to one scheme, host, and port, and to the module's allow-list of `(method, path-regex)` pairs. Before any socket is opened, the path is normalised and **`fullmatch`ed**. Percent-escapes (`%`) and `..` segments are **always refused**, no matter what an entry's pattern would match; only a query string (`?...`) can be opted into, per entry, via `allow_query` (D15). Any other call raises `NotAllowed`, and its message never echoes the raw target. **Redirects are never followed**: a 3xx is **raised** as an `upstream_unexpected` error, with the numeric status kept on the error's `status` attribute so callers such as flavour detection can tell a redirect apart without reading headers (D15). Statuses that ask the caller to go elsewhere (301, 302, 303, 305, 307, 308) report `unexpected redirect (status N)`; other 3xx codes (300, 304) report `unexpected status (status N)`. TLS supports system verification, a CA file (with an optional `server_hostname` for IP hosts), or a pinned SHA-256 leaf fingerprint. The pin is checked right after the handshake, **before any request bytes are sent**. TLS options with the `http` scheme are rejected at construction. `insecure` must be explicit. Caller-supplied `Host` and `Content-Length` headers are refused. Cookies and CSRF tokens are kept in memory only. Bodies are capped at 8 MiB. `Content-Length` must be decimal and non-negative, with every repeated value agreeing; a body shorter than the framing promises is `upstream_unexpected`, never a short success. The deadline is a **total bound**: `ctx.deadline` arrives as `deadline_s`, and connect, handshake, send, headers, and body share it. Two lines of defence enforce it: every stage waits at most the smaller of its configured timeout and the time left, and a watchdog timer shuts the live socket down when the deadline passes, waking whatever `http.client` call is blocked inside a drip — per-call socket timeouts alone cannot bound one, because a single `read` loops `recv` internally. The watchdog holds a `dup` of the request's own socket, fires once, and is cancelled and joined before the request returns, so it can neither touch an unrelated socket nor leave a thread behind; a failure after it fired maps to `timeout`. |
| `core/store.py` | Per-module JSON storage (§3.3), exposed through `ctx.store`. Lazy directories, atomic writes, newest-first listing, id-only loading, and count-based retention. |
| `core/envelope.py` | Builds success and error envelopes, measures the size budget (§5), and sanitises strings. The runner applies the output policy. |
| `core/dropins.py` | Generates one SDK drop-in per registered module command plus the two core commands, and validates the SDK rules (§2.2). Generation and installation run unprivileged except for the individual `sudo install` calls (§3.1). |
| `sdk.py` | The **public module API**. It is the only thing modules import from Housesitter. See [MODULES.md](MODULES.md). |

### 3.1 CLI and drop-ins

| Command | Output and behaviour |
|---------|----------------------|
| `invoke <full name> [--params JSON]` | One compact JSON envelope on stdout. Params come from `--params` when supplied, otherwise stdin; blank input means `{}`. A failed stdin read gives `bad_params`, "cannot read params from stdin". Failure exits 1 and prints the error line last on stderr. |
| `list [--json]` | Sorted commands, including disabled modules and core commands. Text is tab-separated name, status (`enabled`, `disabled`, or `core`), and first sentence of the description. `--json` returns an array of `{name, status, description}`. |
| `describe` | Same as `invoke housesitter.describe` (§3.2). |
| `doctor [--check]` | Same as `invoke housesitter.doctor`, with `{"check": true}` when requested (§3.2). |
| `config-check` | Loads and validates config. Prints `config ok: <path>`, then `<module id>: enabled` or `disabled` per module. Invalid config exits 1 with a `config_invalid` stderr line. |
| `dropins --out DIR [--exec PATH]` | Writes each mode-644 JSON file through an unpredictable temporary file created with `O_EXCL`, then renames it over the destination; a destination symlink is replaced, not followed. Prints sorted filenames. Creates a missing output directory with mode 755. |
| `dropins --print [--exec PATH]` | Prints one JSON object mapping filenames to content objects; writes no files. |
| `install-commands [--exec PATH] [--commands-dir DIR] [--dry-run]` | Generates files in a temporary directory and copies each with `sudo install -o root -g root -m 644`. Destination defaults to `/etc/musegadget/commands.d/`. |

Without `--exec`, generation uses the resolved `sys.argv[0]` only if its
basename is `housesitter`; other entry paths, including `python -m`, need
an explicit path. The path must be absolute, normalised, and free of
whitespace and control characters.

`generate` returns `{filename: content}`. Each file contains
`description`, `exec: [PATH, "invoke", FULL_NAME]`, `required`, `optional`,
and `timeout_ms`. Parameter specs contain only `type` and `description`;
constraints are appended to the description. Files use compact, sorted
JSON with a trailing newline. `validate` checks names and reserved
prefixes, allowed keys, non-blank descriptions, a non-empty string argv
starting with an absolute path, valid parameter names and exact specs,
no required/optional overlap, and timeout bounds of 1..600000 ms.
Housesitter's `Command` declarations require at least 5000 ms.

Each file is limited to 64 KiB. Generation also limits the sum of
serialised UTF-8 file bytes to 88 KiB, Housesitter's share of the SDK's
256 KiB `link.register` message. The SDK silently skips commands that
do not fit that message.

`install-commands` refuses to run as root. `--dry-run` prints the proposed
copies without running sudo. Both modes warn about stale
`housesitter.*.json` files and leave them in place. They print
`sudo systemctl restart musegadget` as the next step, with a warning that
restarting stops any command Muse is running. They do not restart it.

### 3.2 Core commands

Both commands run through the runner and return envelopes with module
`housesitter` and schemas `housesitter.describe/1` or `housesitter.doctor/1`.

`describe` returns `data.modules` and `data.core_commands`. Each module has
`id`, `title`, `version`, `third_party`, `enabled`, `skill`, and `commands`.
Each command has `name`, `description`, `required`, `optional`, and
`timeout_ms`. `required` and `optional` are lists sorted by parameter name,
each entry shaped as `{name, type, description}` with a constraint-bearing
description. `skill` contains the packaged guide text, or null with an
envelope warning if the guide is missing, invalidly named, or over 32 KiB.
Each guide read requests at most 32 KiB + 1 bytes.

`doctor` reports findings in these `data` fields:

| Field | Shape and meaning |
|-------|-------------------|
| `config` | `{path, ok, problem, exists}`. Config has already passed the loader. |
| `secrets` | `{dir, ok, problem, exists, files}`; each file has `{name, ok, problem}`. Checks file type, owner, and permissions without reading secret contents. |
| `data_dir` | `{path, ok, problem, exists}` for the data root: directory type, no symlink, current owner, mode 700. |
| `dropins` | `{dir, files}`; each expected file has `{name, installed, matches, root_owned_644, problem}`. Compares exact generated bytes using the installed exec path. Stale files are not included. |
| `sudo` | `{in_sudo_group}`: membership in `sudo`, `wheel`, or `admin`, using group lookup without a subprocess. This is information, not a test of sudo permissions. |
| `modules` | Entries `{id, enabled, check, problem}`. `check` is null unless requested for an enabled module; then it is the list returned by `Module.check(ctx)`. Failures put a stable error code in `problem`. |
| `problems` | Count of failed findings, including missing expected drop-ins and failed module checks. Sudo membership is not a problem. |

Home paths in config, secrets, and data-root findings use `~`. A missing
config file, secrets directory, or data root is not a problem. Module
check strings are sanitised, capped at 200 characters, and limited to 20
non-empty entries. Findings do not make doctor fail; config/registry
loading, handler timeout, and output limits can still fail the invocation.
`Module.check` receives a read-only store: `list` and `load` work without
creating directories or changing modes. `save` fails with `config_invalid`,
"doctor checks are read-only". Exceptions from `Module.check` set that
module's `problem` to a stable code: command, HTTP, and store errors keep
their codes, disallowed HTTP calls become `not_allowed`, secret-read
failures become `secret_unreadable`, and other exceptions become
`upstream_unexpected`. Exception text is not returned.

Installed drop-in reads use non-blocking, no-follow opens and accept only
regular files. They read at most 64 KiB + 1 bytes, so a huge file is never
loaded whole. Oversized files get the problem
"file is larger than 64 KiB".

### 3.3 Store

Data lives at `$HOME/.local/share/housesitter/<module_id>/`. The data root
and module directory are created lazily on the first save, with mode 700;
`list` and `load` never create directories or change modes. Symlinked and
foreign-owned roots or module directories are refused. Saves tighten both
the data root and module directory to 700 when they have extra permission
bits.

`save(kind, obj, label=None)` writes compact JSON to a mode-600 temporary
file, flushes it to disk, then hard-links it to the final name and removes
the temporary file. Name collisions retry with another suffix, up to 16
attempts; an existing result is never overwritten. Ids have the form
`<kind>_<YYYYMMDDTHHMMSSZ>_<4 lowercase hex>`, without `.json`.
The timestamp is the save time in UTC. The object itself supplies the
`collected_at` and `label` metadata used by listing; the separate `label`
argument does not add metadata. After linking the final file, the store
fsyncs the module directory on a best-effort basis.

Retention counts saved files across all kinds within each module. After
each save it prunes the oldest by filename timestamp, with id as a
tie-breaker. Only store-shaped `*.json` names are candidates; other files
are left alone. Deletion is best effort. The default is 500, overridden by
`modules.<id>.retention`; 0 keeps everything (D6).
After a save, cleanup also removes regular `.tmp-*.json` leftovers whose
modification time is more than one hour before the save time. This is best
effort and skips symlinks and non-regular files.

`list(kind, offset=0, limit=20)` returns `StorePage(total, items)`, newest
first, with id as a tie-breaker. Offset is non-negative and limit is
1..100. Each item contains id, kind, and optional collected-at/label
strings read from the file. Collected-at text must have the exact shape
`YYYY-MM-DDTHH:MM:SSZ` (calendar validity is not checked), and labels are
limited to 200 characters, matching `StoredItem` validation. Damaged
metadata becomes null per field; an unreadable or corrupt file still
lists with both values null. Store file opens use `O_NONBLOCK` and
`O_NOFOLLOW`; reads check the open descriptor is a regular file, while
saves create a new file exclusively with `O_EXCL`.
`load(id)` accepts only store-shaped ids, caps reads at 32 MiB, and requires
a JSON object. Invalid, missing, unreadable, oversized, or corrupt results
raise `not_found`; unsafe directories and failed writes raise
`config_invalid`. Reasons never contain file contents.

### 3.4 Runner

1. Resolve the full command name, load config, and reject disabled modules.
2. Parse at most 1 MiB of UTF-8 JSON params. Reject non-objects, unknown
   keys, missing or null required params, wrong types, non-finite numbers,
   enum violations, numeric bounds, and string length violations as
   `bad_params`. Booleans do not count as numbers. Null optional params
   count as absent; non-null defaults fill absent optionals.
3. Build a Context with the module's store and a self-deadline measured
   from invocation start: command timeout minus 3 seconds.
4. Call the handler in a worker thread and wait only until that deadline.
   An overrun returns `timeout`; the CLI flushes the error envelope and
   stderr line, then exits the process if the worker is still alive.
   `CommandError`, `UpstreamError`, and store errors retain their stable
   codes; `NotAllowed` becomes `not_allowed`. Unexpected exceptions and
   non-`Result` returns become `upstream_unexpected`, without exception text.
5. Sanitise the label (at most 200 characters), combine handler and Context
   warnings, and sanitise declared untrusted fields in data, source, and
   compact data. Warnings are capped at 200 characters each, de-duplicated,
   and limited to 20; on overflow the last says how many more were omitted.
6. If `Result.save` is set, save the sanitised full result before output
   projection. This preserves identifiers and undeclared keys locally.
7. Apply identifier policy, drop undeclared keys, and check declared types
   in data, source, and compact data (MODULES §2). A mismatch becomes
   `upstream_unexpected` and names the field path.
8. Fit the envelope to the budget: full data, then compact data, then
   truncation in `Paging.lists` order (§5). If nothing fits, return
   `upstream_unexpected`.
9. Return the envelope. On failure the CLI also prints the stable error
   line last on stderr (§4). Records under the `housesitter` logger never
   fall through to Python's last-resort stderr handler, so late worker logs
   after a timeout do not displace the final error line.

Unsafe data directories or disabled modules produce `config_invalid`.
Unknown commands produce `not_found`; the name is echoed only if it
matches the dotted command-name syntax and is at most 64 characters.

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

Envelope and saved-file serialisation refuse NaN and Infinity. Neither can
reach stdout or a saved file; the command fails with `upstream_unexpected`.
An invalid envelope reports "the result is not valid JSON".

Stable error codes: `config_invalid`, `secret_unreadable`, `upstream_auth`,
`upstream_unreachable`, `upstream_tls`, `upstream_unexpected`, `not_allowed`,
`timeout`, `bad_params`, `not_found`.

## 5. Size budget and paging

- The envelope is serialised compactly and measured with the SDK's own
  formula, `len(json.dumps(text)) - 2`. That measure must be ≤ **88 KiB**,
  leaving headroom under the SDK's 96 KiB cut. **Planned (12c):** conformance will assert
  the measure on the large and hostile fixtures, the latter heavy in
  non-ASCII.
- If full data does not fit, the runner tries `Result.compact`. A fitting
  compact form still has `truncated: false` and `more: null`.
- If that does not fit, it cuts top-level lists from the end in
  `Paging.lists` order, starting from compact data if supplied. It keeps
  as much of the first list as fits; only after emptying it does it shrink
  the next. It sets `truncated: true`.
- `more` has this shape (the placeholders below describe values):

  ```text
  {
    "command": "housesitter.<module>.<paging command>",
    "params": {
      ...Paging.params,
      "id": <saved id, otherwise Paging.params["id"]>,
      "section": <first truncated list>,
      "offset": <Paging.start for that section, default 0, plus items kept>
    },
    "remaining": {<each truncated list>: <items cut>}
  }
  ```

  `params` is the exact call to replay. `Paging.params` rejects `section`,
  `offset`, and `remaining` at construction; `id` is allowed. The runner
  sets `section` and `offset`, and overwrites `id` when saved; `remaining`
  sits beside `params`.
  Before returning truncated output, the runner checks that
  `housesitter.<module>.<paging command>` exists and declares `section`
  (string), `offset` (integer), and `id` (string), as required or optional
  params. The replay must have a non-empty string `id`. If these checks
  fail, the command fails with `upstream_unexpected`, "the module's paging
  cannot fetch the rest of this result". Other replay params and parameter
  constraints are not checked at this stage; authors must make the call
  replayable. Core-command targets instead use `housesitter.<command>`
  and are validated against the core command table.
- When `Result.save` is set, the saved file keeps the full result even
  when output is compacted or truncated. Saving is optional for results
  that fit. A paging command can page its own output without saving again
  by carrying the existing id in `Paging.params["id"]`; the current call
  then creates no new saved file.

## 6. Files, config, and secrets

Everything lives under the SDK's run-as account:

| Path | Mode | Contents |
|------|------|----------|
| `~/.config/housesitter/config.json` | 600 recommended, not enforced | Core and per-module config. No secrets, but names the secret files. |
| `~/.config/housesitter/secrets/<name>` | Owner-only, e.g. 600 or 400 | One secret per file, e.g. `unifi_password`; owned by the current account, with no group or world permission bits. |
| `~/.config/housesitter/secrets/` | 700 (checked by doctor) | Must be a non-symlink directory owned by the current account. The secret reader checks files, not the directory. |
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
  `none`, every declared identifier value is left out of returned output,
  including nulls. No hashing.
- Saved local files hold full values before output projection. Paging
  handlers run through the same identifier policy as collection handlers.
- Secrets are a different matter from identifiers: they never appear in
  output under any setting (AGENTS rule 6).
- Names, SSIDs, and AP names are returned, because Muse needs them to talk
  about the house. They are sanitised (rule 8):
  - Unicode categories `Cc`, `Cf`, and `Cs` (control, bidi,
    zero-width, and lone surrogates) are stripped.
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
| The SDK's run-as account has sudo, so Muse has root. | The owner decides this. It is outside Housesitter's control. README explains the trade-off, and `doctor` reports (as information, not a failure) whether the account belongs to a sudo-related group. |
| The allow-list is bypassed by an HTTP redirect, or by a path trick (`stat/device-basic`, `%2e%2e`, `?`). | No redirects: a 3xx raises `upstream_unexpected` with its numeric status (D15). `fullmatch` on the normalised path. `%` and `..` are always refused, even under a permissive pattern; `?...` only when the entry sets `allow_query` (D15). **Planned (12c):** conformance will test each case. |
| A slow upstream drips bytes to defeat the deadline. | The deadline is a hard total bound: shrinking per-stage socket timeouts plus a watchdog that shuts the live socket at the deadline, so any drip fails as `timeout` within the deadline plus a small margin (§3). |
| A module writes to upstream. | Prevented structurally by the HTTP allow-list. **Planned (12c):** conformance will test this, and the module's threat review checks it. |
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
