# Muse Housesitter — merged dashboard

The owner selected C's change-first dashboard, A's detailed measurements, and
B's e-ink composition. This implementation changes only `web/`. It fetches saved
results; reload never collects data. There is no build step or external asset.

## Sources and layout

**C — overall design and structure.** Warm paper, forest-green rules, serif
headline, system fonts, dark-mode palette, BEFORE / AFTER snapshot panels,
ID-matched moves and new/gone devices, numerical comparisons, “What stayed”,
attributed Muse note, saved-moment timeline, inventory, and glossary. “What
changed” remains the first main content section. Additional signal/rate changes
retain C's disclosure; measurement details in the inventory do not require one.

**A — measurement detail.** Each AP shows every radio's band, channel, width in
MHz, utilization and reference label, controller client count, retries, power,
self receive/transmit airtime, and satisfaction. AP metadata and all associated
clients appear directly below. Client rows show signal in dBm with its reference
label, both negotiated rates in Mbps, cumulative retries with its reference
label, band, channel, SSID, and identity. Clients with no AP appear in their own
“AP unknown” group. All 600 large-fixture clients are visible in these groups.
The separate searchable, paged ledger exposes all other reported client fields
without disclosures; it scrolls inside a keyboard-focusable region on phones.
Controller counts and counts derived from the saved client list stay distinct.

**B — e-ink.** A separate 800 × 480 sheet uses only black and white CSS colours,
a compact status headline, all APs in two columns, and three clients ordered by
lowest reported signal (unknowns last). A literal “+ N more devices” accounts for
omitted clients. Unknown-AP counts share that line. Snapshot labels, newest and
previous history timestamps, a Muse excerpt, and definitions remain visible.
There are no hover dependencies, animations, grey colours, or font-smoothing
tricks. Browser font rasterization is left to the renderer.

E-ink long labels use DOM text fitting against actual font metrics, with an
ellipsis and the full sanitized text in accessible labels/title attributes.
The Muse note gets two complete lines, or one when both a collection error and
warning need space. Neither line-clamping nor hidden overflow conceals content.
The dashboard retains the longer note and the full supplied recent history.

## States

- **typical:** before/after device moves lead; 50 of 60 saved moments are shown,
  with the supplied Muse note and current AP/client readings.
- **empty:** no saved moments, with a never-collected explanation; unknown data
  is not replaced by invented zero readings.
- **error:** last-attempt error is visible above retained last-good data and age.
- **failed:** collection error plus an explicit no-successful-snapshot state.
- **large:** all eight APs, offline state, nullable radio readings and 600
  clients; e-ink shows three clients plus 597 more.
- **hostile:** names, labels, warnings, errors, and notes remain literal text.
  Control/bidi characters are removed and displayed strings are length-capped.
  ID lookups use Map; data never enters HTML, URLs, styles, or executable code.
- **edge:** unknown readings, unknown band and missing AP are explicit;
  controller counts remain separately identified. An unlabelled previous
  snapshot remains a previous snapshot.

Data age uses generated_at minus collected_at. Client/AP comparisons use IDs.
Reference labels use the contract's ranges, without inferred diagnoses or advice.
The glossary explains all eight required radio terms. Disabled WiFi produces no
WiFi inventory/error. HTTP failures, invalid JSON and unknown schema versions
show a plain error while preserving previously loaded content and its age.

## Six bakeoff fixes

1. **360 px overflow:** constrain grid/flex intrinsic widths, wrap long text and
   tags, use safe AP column minima, and keep the ledger's scroll within its panel.
2. **E-ink history/glossary overflow:** separate bounded rows, measured text
   fitting, compact history, and wrapping glossary; no hidden overflow. The
   final checker also checks visible internal overflow and screen boundaries.
3. **Clipped Muse note:** fit complete lines and add a visible ellipsis; the
   dashboard holds the longer note. No partial line is cut off.
4. **Latest/Previous separator:** an explicit ` | ` separates both labels and
   history timestamps; dashboard snapshot times are separate blocks.
5. **Unit casing:** signal and power consistently use `dBm`.
6. **Hidden radio details:** radio/AP measurements and client inventory details
   use ordinary visible DOM blocks, not expanders.

## Verification

Evidence is generated from invented fixtures only. Adapted screenshot runner:
`/tmp/hs-merge-shoot.py` (original bakeoff tool unchanged). Screenshot directory:
`/tmp/hs-merge-shots/`; machine-readable matrix: `report.json` there. It captures
phone 360 × 780, desktop 1280 × 900, desktop dark at the same size, and e-ink
800 × 480, for all seven fixtures. It records console/page errors, dialogs,
page-width overflow, internal hidden/clip overflow, and e-ink internal overflow
and bounds. Initial rendered checks found count-line fitting, client/history
overlap, and edge/hostile e-ink layout defects; these were fixed before the final
matrix. The original bakeoff captures remain at `/tmp/bakeoff/shots/` for before
evidence.

Additional browser checks (`/tmp/hs-merge-behavior.py`) verify both pages preserve
prior data on HTTP error, invalid JSON, and unknown schema; hostile fields create
no script, image, iframe or style elements; all 600 clients appear by AP; search
and next-page navigation work at 360 px. JavaScript syntax is checked with Node.
Python 3.9.25: all 124 tests and ruff pass, including dashboard-contract and hygiene
tests. This checkout has no separate conformance directory. No live system,
hardware, SDK, or real-house data was used. All temporary servers are stopped.

Review gates: local automated checks pass; CI has not run because nothing was
committed or pushed. Independent agent and spec-vs-code reviews remain for the
parent task: this design sub-agent has not reviewed itself independently or
spawned other agents. No core HTTP, storage, secrets, identifiers policy, install,
or module schema changed. Local hostile-input checks passed; independent threat
review and Jon's merge sign-off remain with the parent workflow as applicable.
No commit or push was made.

## Contract observations

No blocking contract change is needed. The written signal intervals overlap at
−60 and −70; first-applicable precedence is strong ≥ −60, good ≥ −70, weak
≥ −75, then very weak. The 800 × 480 format requires explicit excerpts, so
e-ink keeps only latest/previous timestamps and a shortened Muse note while the
main dashboard retains the longer content. Docs outside `web/` still describe
the front end as Planned; updating those is outside this task's allowed scope.

Final matrix: each cell is **errors / page overflow / internal clipping**, all
zero. E-ink additionally has zero internal overflow and zero elements outside
800 × 480. Chromium enforces the dev server's CSP and Trusted Types throughout.

| Fixture | Phone | Desktop | Dark desktop | E-ink |
| --- | --- | --- | --- | --- |
| typical | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| empty | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| error | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| failed | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| large | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| hostile | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |
| edge | 0/0/0 | 0/0/0 | 0/0/0 | 0/0/0 |

## Palette

Slate / Blue, chosen by Jon from four options (2026-11-06) to replace the
original tan/olive. Only the CSS variables in `dashboard.css` changed: light
`:root` and the `prefers-color-scheme: dark` block. Every text/surface pair
meets WCAG AA (lowest text pair 6.47:1 light, 6.55:1 dark; borders at least
3.1:1). Colour never carries meaning on its own; the reference words do.
The e-ink page stays pure black and white.
