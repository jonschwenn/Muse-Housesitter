# Muse Housesitter

Have you ever wanted to give Claude, or really any good LLM, the ability to help you configure complex systems you run that may be working, but far from optimal? The stupid thing would be to give an LLM Agent direct access to your Unifi or Home Assistant portal. Instead the better approach is to write scripts to gather data to analyze which the LLM Agent can use to make recommendations or help with config examples.

Along comes Muse, which is a helpful little agent running somewhere constantly on a VM. It's pretty useful if you trust Meta and connect it to things. I wouldn't say I trust it completely, but it has a really interesting functionality of 'Gadgets'. You can load the Gadgets SDK on a Raspberry Pi for example and Muse will be able to control the Pi. For example, it could write and run scripts, access things within your home network, etc. 

I have a Pi on my IoT VLAN and I use it to look at radio/WiFi stats using a read-only Unifi user. I plan on extending this to other things and will be adding modules over time.

Regardless of your feelings on AI, please understand that this is a human-run project using tools for code testing and release velocity. Low-quality submissions will not be accepted. 

---

Housesitter gives [Muse](https://gadgets.muse.ai) eyes on the house. It is a
small, read-only Python tool that runs on a Raspberry Pi next to the
[Muse Linux gadget SDK](https://github.com/facebookincubator/muse-gadget-sdk/tree/main/linux).
It gathers measurements from systems in the home and hands them to Muse as
commands Muse can call. Muse does the analysis and makes the
recommendations, and you decide what to change.

> **Status: design stage.** There is no code yet. The design is in
> [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and the first module's spec
> is in [`docs/modules/unifi_wifi.md`](docs/modules/unifi_wifi.md).

## Modules

| Module | Gathers | Status |
|--------|---------|--------|
| `unifi_wifi` | WiFi radio metrics from a local UniFi controller: APs, channels, channel load, client signal, negotiated rates, and retries. | Spec proposed |

New data sources are added as modules. See [`docs/MODULES.md`](docs/MODULES.md).

## Principles

- **Read-only.** Housesitter never changes anything it observes.
- **Gathers, doesn't judge.** It returns labelled measurements with units,
  and Muse interprets them.
- **On demand.** Nothing runs in the background. Muse calls it when it wants
  to look.
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

## License

MIT. See [`LICENSE`](LICENSE).
