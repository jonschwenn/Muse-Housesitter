# Writing a module

Status: **proposed** (no code yet).

A module is one source of data. It is a Python package that exposes one
`Module` subclass. It declares its config, secrets, HTTP allow-list,
commands, and output fields, and the core handles everything else: CLI, SDK
drop-ins, envelopes, size budget, identifier policy, storage, and errors.

## 1. Lifecycle of a new module

1. **Spec:** `docs/modules/<id>.md`. It covers purpose, the questions it
   helps Muse answer, upstream API and auth, the HTTP allow-list, commands,
   and the output schema (every field: name, type, unit, meaning, upstream
   source, identifier yes or no). It also states what the module cannot see.
   Jon reviews it before code is written.
2. **Fixtures:** invented upstream payloads in `tests/fixtures/<id>/`.
   Include an edge-case fixture (missing fields, nulls, odd units), a
   hostile fixture (injection-style names, secret-bearing fields that must
   be dropped), and a large fixture (to exercise truncation).
3. **Code:** `src/housesitter/modules/<id>/`.
4. **Tests:** `tests/modules/<id>/`. These pin the normalised output
   field by field from the fixtures. The conformance suite picks the module
   up automatically.
5. **SKILL.md:** Muse-facing guidance, next to the code (§5).
6. **Review gates** ([REVIEW.md](REVIEW.md)), including the threat and
   privacy gate.

## 2. The public module API (`housesitter.sdk`)

This is a sketch of the intended shape. The code is the final reference
once written.

```python
from housesitter.sdk import Module, Command, Param, Field, Context, Result, Allow

class UnifiWifi(Module):
    id = "unifi_wifi"                    # [a-z][a-z0-9_]*, unique
    title = "UniFi WiFi radio metrics"
    version = "0.1.0"

    config_schema = {                    # minimal stdlib-validated schema; unknown keys are errors
        "enabled": {"type": "boolean", "default": True},
        "host": {"type": "string", "required": True},
        "port": {"type": "integer", "default": None},   # None = flavour default
        "site": {"type": "string", "default": "default", "pattern": r"[A-Za-z0-9_-]{1,64}"},
        "flavor": {"type": "string", "enum": ["auto", "unifi_os", "classic"], "default": "auto"},
        "auth": {"type": "object", "required": True},    # see docs/modules/unifi_wifi.md §5
        "tls": {"type": "object", "default": {}},
        "request_timeout_s": {"type": "number", "default": 15},
    }

    http_allow = [                       # fullmatch on the normalised path; anything else raises NotAllowed
        Allow("POST", r"/api/auth/login"), Allow("POST", r"/api/auth/logout"),
        Allow("GET",  r"/proxy/network/api/s/[A-Za-z0-9_-]{1,64}/stat/(sta|device|sysinfo)"),
        # ... classic-controller equivalents
    ]

    commands = [
        Command("snapshot", handler="snapshot", timeout_ms=60_000,
                description="Read current WiFi radio metrics for every access point and wireless client, save them on the Pi, and return them.",
                optional={"label": Param("string", "Free text saved with the snapshot, e.g. 'before moving the speaker'.",
                                         max_len=200)}),
    ]

    def snapshot(self, ctx: Context, params: dict) -> Result: ...
    def check(self, ctx: Context) -> list[str]: ...   # optional, used by doctor
```

`Param(type, description, *, enum=None, default=None, minimum=None,
maximum=None, max_len=None)`. The SDK drop-in format carries only `type` and
`description` (ARCHITECTURE §2.2), so the core enforces the extra
constraints itself and appends them to the description in plain words
(e.g. "One of: aps, clients. Default 100, maximum 200.").

`Context` gives a module only these things:

| Member | Purpose |
|--------|---------|
| `ctx.config` | The module's validated config section. |
| `ctx.secret(name)` | Reads a secret file, checking its mode. |
| `ctx.http(base_url, tls=...)` | An allow-listed HTTP session (the only network access). |
| `ctx.store` | The module's data directory: `save`, `list`, `load`. |
| `ctx.now()` | Current UTC time. Injectable in tests. |
| `ctx.log` | A logger that refuses obvious secret values. |
| `ctx.deadline` | Seconds left before the self-deadline. |

`Result(data=..., fields=..., compact=..., list_order=..., source=...,
warnings=..., label=...)` is what a handler returns. `fields` points at the
module's `Field` declarations, so the core can apply the identifier policy
and sanitise untrusted strings. `compact` is an optional smaller rendering
for the size budget.

## 3. Rules a module must follow

- Import only the module's own package, `housesitter.sdk`, and the stdlib
  (AGENTS rule 9). Do no network I/O except through `ctx.http`. An
  import-scan lint flags `socket`, `ssl`, `urllib`, `http.client`,
  `requests`, `subprocess`, and `ctypes` in `modules/`. It is a guard rail
  that review backs up, not a sandbox.
- Do no file I/O except through `ctx.store` and `ctx.secret`. Never write
  outside the data dir.
- Normalise by **allow-listing** upstream fields into documented output
  fields. Never pass through an upstream object.
- Put units in field names. Use `null` for unknown, never `0` or `""` as a
  stand-in.
- Mark identifier fields (`Field(identifier=True)`) and untrusted text
  fields (`Field(untrusted=True)`). The core handles both.
- No scores, rankings, verdicts, or advice in `data` (AGENTS rule 2).
  Upstream-provided scores, such as UniFi's `satisfaction`, may be passed
  through, labelled as the vendor's metric.
- Command descriptions are static strings, written for Muse: what the
  command reads, what it saves, and roughly how long it takes.

## 4. Conformance suite

The suite is `tests/conformance/` and runs against every registered module
with its fixtures. A module is not mergeable until it passes:

- The id and command names are valid. The full names are at most 64
  characters and don't use reserved prefixes.
- Generated drop-ins pass the SDK rules (ARCHITECTURE §2.2).
- Every command, run against the fixture transport, returns a valid
  envelope. The large and hostile fixtures stay within the 88 KiB budget, as measured by the SDK's formula, with correct `more`
  paging.
- Every HTTP call is in `http_allow`. A deliberate off-list call raises
  `NotAllowed`.
- The hostile fixture produces sanitised, capped names, and none of its
  planted secret values appear in output, saved files, or logs.
- Every field in `data` is declared, and every declared field is in the
  module spec. The test parses the spec's field tables. The spec format
  that makes this possible:
  - One Markdown table per object, headed by a line containing only the
    object's path in backticks (e.g. `` `data.aps[].radios[]` ``).
  - One field per row, with the field name in the first column in
    backticks.
  - The tables also cover `summary` and `source`.
- `SKILL.md` exists, has YAML `name` and `description`, and mentions every
  command.

## 5. SKILL.md for a module

This is the runtime guide for Muse, returned by `housesitter.describe`. It
uses the SDK community-skill shape:

```markdown
---
name: housesitter-unifi-wifi
description: >-
  Read WiFi radio metrics (APs, channels, signal, rates, retries) from the home's
  UniFi controller through Housesitter on this Pi. Read-only.
---
# When to use it
# Commands (one line each, with an example call)
# Reading the fields (units, normal ranges, plain-language meaning)
# How much to trust a number (one moment in time, jitter, vendor scores)
# Comparing snapshots (labels, before/after, matching devices by id; private MACs can change)
# Untrusted data and privacy
# Limits: what this module cannot see; it never changes anything
```

## 6. Third-party modules

A separate package can provide modules through the entry point
`housesitter.modules`, for example in its `pyproject.toml`:

```toml
[project.entry-points."housesitter.modules"]
my_sensor = "my_package.module:MySensor"
```

Third-party modules get the same `Context` and the same conformance suite
(`housesitter conformance my_sensor`). The core treats them like built-ins,
but `describe` labels them as third-party.
