# Muse Housesitter

**An independent, unofficial personal project by Jon Schwenn.** Muse
Housesitter is not affiliated with, sponsored by, endorsed by, or maintained
by Meta Platforms, Inc. or its affiliates. See
[attribution and independence](ATTRIBUTION.md) for upstream credits and
third-party names.

I started this project to give Muse useful measurements from systems in my
home without giving Housesitter permission to change those systems. The
first planned module gathers WiFi radio statistics from UniFi using a View
Only account. Muse can interpret those measurements and suggest changes;
the owner decides what to apply.

This is a human-run project that uses AI tools for development, testing,
and review. Contributions follow the same review gates regardless of who
or what wrote them.

---

Housesitter gives [Muse](https://gadgets.muse.ai) eyes on the house. It is a
small, read-only Python tool that runs on a Raspberry Pi next to the
[Muse Linux gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/main/linux).
It gathers measurements from systems in the home and hands them to Muse as
commands Muse can call. Muse does the analysis and makes the
recommendations, and you decide what to change.

> **Status: early.** The core runs commands: runner, store, drop-ins, and
> CLI (`invoke`, `list`, `describe`, `doctor`, `dropins`, `install-commands`,
> `config-check`). No data module or conformance suite yet. See
> [the architecture](docs/ARCHITECTURE.md) and
> [the first module's spec](docs/modules/unifi_wifi.md).

`housesitter describe` lists modules and commands; each command's required
and optional parameters are lists sorted by name, with a name, type, and
description per entry. `housesitter doctor` reports local setup findings;
add `--check` to run enabled modules' read-only checks. Those checks can
list and load saved results, but cannot save them or create data directories.
Check failures appear as stable error codes in each module's `problem` field;
other findings remain available in the same report.

## Modules

| Module | Gathers | Status |
|--------|---------|--------|
| `unifi_wifi` | WiFi radio metrics from a local UniFi controller: APs, channels, channel load, client signal, negotiated rates, and retries. | Spec proposed |

New data sources are added as modules. See [`docs/MODULES.md`](docs/MODULES.md).

## Dashboard

**Planned.** A read-only dashboard in your browser, served by Housesitter on
the Pi. It shows what Muse last collected, and may later drive an e-ink
display. By default it listens on `127.0.0.1:8080`, so only the Pi itself can
see it. To view it from other devices, set a LAN address in the config. It
is read-only and has no login, so **anyone who can reach that address can
see your device names, MACs, and IPs.** Choose an address only trusted
devices can reach. See [`docs/DASHBOARD.md`](docs/DASHBOARD.md).

## Principles

- **Read-only.** Housesitter never changes anything it observes.
- **Gathers, doesn't judge.** It returns labelled measurements with units,
  and Muse interprets them.
- **Muse decides when to look.** Housesitter installs no schedules. Muse
  runs a module whenever it wants fresh data, and analyses the changes
  over time itself.
- **Personal use.** It's built for your own network and your own Muse.
  Results include real device names, MACs, and IPs, so Muse can match them
  to what you see in UniFi.
- **Secrets stay on the Pi.** Passwords live in owner-only files and never
  appear in output, logs, or saved snapshots.

> **About sudo.** You need sudo to *install*: the SDK installer and
> Housesitter's one-time `install-commands` step both write to system
> locations. Separately, the SDK asks which account Muse runs commands as
> (`--run-as`). If that account can use sudo, so can Muse. Whether to allow
> that is your call. Housesitter never needs sudo while it runs. Either way,
> give Housesitter a **View Only** UniFi account of its own.

## Contributing

Read [`AGENTS.md`](AGENTS.md) first. It holds the rules for testing,
review gates, and documentation, and it applies to people and agents alike.
For announcements, demos, and responses to affiliation questions, see the
[public communication guide](docs/COMMUNICATIONS.md).

## Independence, privacy, and support

Housesitter runs locally, but **results returned to Muse through the SDK
leave the Pi for Muse's service**. Module results can include device names,
network names, MACs, and IPs. Read-only access limits changes to the systems
Housesitter observes; it does not make the integration offline or control
what Muse can do through the SDK's other commands. Review the
[Gadget SDK Token Terms](https://gadgets.muse.ai/sdk-terms),
[Muse terms](https://muse.ai/terms), and
[Muse privacy policy](https://muse.ai/privacy) before connecting your network.

Report Housesitter bugs and documentation or attribution concerns through
[this repository's issues](https://github.com/jonschwenn/Muse-Housesitter/issues).
Use invented examples: never post credentials or real house data. Meta and
Ubiquiti do not provide support for this project. See
[ATTRIBUTION.md](ATTRIBUTION.md) for upstream credits and the separate
software licenses and service terms.

## License

MIT. See [`LICENSE`](LICENSE).
